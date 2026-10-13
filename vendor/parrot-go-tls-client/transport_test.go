package main

import (
	"context"
	"encoding/pem"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func localServer(t *testing.T, handler http.HandlerFunc) (string, clientConfig) {
	t.Helper()
	server := httptest.NewUnstartedServer(handler)
	server.EnableHTTP2 = true
	server.StartTLS()
	t.Cleanup(server.Close)
	return server.URL, clientConfig{CAPEM: string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}))}
}
func requestFor(t *testing.T, cfg clientConfig, in requestIn) *requestState {
	t.Helper()
	cid := newClient(cfg)
	t.Cleanup(func() { closeClient(cid) })
	rid, err := startRequest(cid, in)
	if err != nil {
		t.Fatal(err)
	}
	return getRequest(rid)
}
func nextKind(t *testing.T, s *requestState, kind string) event {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		e := s.poll(50 * time.Millisecond)
		if e.Kind == kind {
			return e
		}
		if e.Kind == "error" || e.Kind == "eof" {
			t.Fatalf("expected %s, got %+v", kind, e)
		}
	}
	t.Fatal("event wait expired")
	return event{}
}
func TestStreamingCancelAndCleanup(t *testing.T) {
	exited := make(chan struct{})
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) {
		defer close(exited)
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	s := requestFor(t, cfg, requestIn{Method: "POST", URL: url, Body: []byte("{}"), ConnectTimeout: 1})
	nextKind(t, s, "dispatch")
	nextKind(t, s, "uploaded")
	nextKind(t, s, "headers")
	if e := nextKind(t, s, "data"); string(e.Data) != "data: first\n\n" {
		t.Fatal(e)
	}
	s.cancel()
	closeRequest(s.id)
	if !s.terminal.Dispatched || s.terminal.Code != "cancelled" {
		t.Fatal(s.terminal)
	}
	select {
	case <-exited:
	case <-time.After(time.Second):
		t.Fatal("cancel did not stop server IO")
	}
	if getRequest(s.id) != nil {
		t.Fatal("request handle leaked")
	}
}
func TestRejectCertificateBeforeDispatch(t *testing.T) {
	url, _ := localServer(t, func(w http.ResponseWriter, r *http.Request) { t.Error("untrusted request dispatched") })
	s := requestFor(t, clientConfig{}, requestIn{Method: "GET", URL: url})
	e := nextKind(t, s, "error")
	if e.Dispatched || e.Code != "certificate" {
		t.Fatal(e)
	}
}
func TestNoRedirect(t *testing.T) {
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			t.Error("redirect followed")
		}
		w.Header().Set("Location", "/next")
		w.WriteHeader(302)
	})
	s := requestFor(t, cfg, requestIn{Method: "POST", URL: url, Body: []byte("{}")})
	if e := nextKind(t, s, "headers"); e.Status != 302 {
		t.Fatal(e)
	}
	nextKind(t, s, "eof")
}
func TestCancelBlockedTLSAndConnectTimeout(t *testing.T) {
	for _, cancelNow := range []bool{true, false} {
		t.Run(map[bool]string{true: "cancel", false: "timeout"}[cancelNow], func(t *testing.T) {
			ln, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			defer ln.Close()
			accepted := make(chan struct{})
			ended := make(chan struct{})
			go func() {
				defer close(ended)
				c, e := ln.Accept()
				if e != nil {
					return
				}
				defer c.Close()
				close(accepted)
				io.Copy(io.Discard, c)
			}()
			s := requestFor(t, clientConfig{}, requestIn{Method: "GET", URL: "https://" + ln.Addr().String(), ConnectTimeout: .1})
			<-accepted
			if cancelNow {
				s.cancel()
			}
			e := nextKind(t, s, "error")
			if e.Dispatched {
				t.Fatal(e)
			}
			if cancelNow && e.Code != "cancelled" || !cancelNow && e.Code != "connect_timeout" {
				t.Fatal(e)
			}
			select {
			case <-ended:
			case <-time.After(time.Second):
				t.Fatal("TLS socket survived cancellation")
			}
		})
	}
}
func TestReadTimeoutAndConcurrentClose(t *testing.T) {
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	cid := newClient(cfg)
	defer closeClient(cid)
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rid, err := startRequest(cid, requestIn{Method: "GET", URL: url, ConnectTimeout: 1, ReadTimeout: .1})
			if err != nil {
				t.Error(err)
				return
			}
			s := getRequest(rid)
			nextKind(t, s, "headers")
			e := nextKind(t, s, "error")
			if !e.Dispatched || e.Code != "read_timeout" {
				t.Error(e)
			}
			closeRequest(rid)
		}()
	}
	wg.Wait()
	closeClient(cid)
	handles.Lock()
	_, exists := handles.clients[cid]
	handles.Unlock()
	if exists {
		t.Fatal("client leaked")
	}
}
func TestRequireResolver(t *testing.T) {
	d := resolvedDialer{required: true}
	if _, err := d.DialContext(context.Background(), "tcp", "never-resolve.invalid:443"); err == nil {
		t.Fatal("missing DNS policy accepted")
	}
}

func TestCancelProxyNegotiation(t *testing.T) {
	for _, scheme := range []string{"http", "socks5"} {
		t.Run(scheme, func(t *testing.T) {
			ln, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			defer ln.Close()
			accepted := make(chan struct{})
			ended := make(chan struct{})
			go func() {
				defer close(ended)
				c, e := ln.Accept()
				if e != nil {
					return
				}
				defer c.Close()
				close(accepted)
				io.Copy(io.Discard, c)
			}()
			s := requestFor(t, clientConfig{Proxy: scheme + "://" + ln.Addr().String()}, requestIn{Method: "GET", URL: "https://no-local-dns.invalid/", ConnectTimeout: 10})
			<-accepted
			s.cancel()
			e := nextKind(t, s, "error")
			if e.Dispatched || e.Code != "cancelled" {
				t.Fatal(e)
			}
			select {
			case <-ended:
			case <-time.After(time.Second):
				t.Fatal("proxy negotiation socket leaked")
			}
		})
	}
}

func TestCancelBackpressureAndCloseAll(t *testing.T) {
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) {
		block := make([]byte, 32768)
		for i := 0; i < 256; i++ {
			if _, err := w.Write(block); err != nil {
				return
			}
			w.(http.Flusher).Flush()
		}
	})
	cid := newClient(cfg)
	for i := 0; i < 8; i++ {
		_, err := startRequest(cid, requestIn{Method: "POST", URL: url, Body: []byte("{}")})
		if err != nil {
			t.Fatal(err)
		}
	}
	// Deliberately don't drain even the lifecycle queue.
	time.Sleep(50 * time.Millisecond)
	done := make(chan struct{})
	go func() { closeClient(cid); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("close blocked behind consumer backpressure")
	}
	handles.Lock()
	defer handles.Unlock()
	for _, s := range handles.requests {
		if s.owner.config.CAPEM == cfg.CAPEM {
			t.Fatal("active request leaked")
		}
	}
}
