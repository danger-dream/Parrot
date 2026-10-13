// Local-only regression fixtures adapted from the independent native audit.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/net/http2"
	"golang.org/x/net/http2/hpack"
)

func auditDrain(t *testing.T, s *requestState) ([]event, event) {
	t.Helper()
	var es []event
	until := time.Now().Add(5 * time.Second)
	for time.Now().Before(until) {
		e := s.poll(10 * time.Millisecond)
		if e.Kind == "error" || e.Kind == "eof" {
			return es, e
		}
		if e.Kind != "pending" {
			es = append(es, e)
		}
	}
	t.Fatal("drain stalled")
	return nil, event{}
}
func auditRaw(t *testing.T, handler func(net.Conn, *http2.Framer)) (string, clientConfig, *atomic.Int64) {
	t.Helper()
	base := httptest.NewUnstartedServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	base.EnableHTTP2 = true
	base.StartTLS()
	cert := base.TLS.Certificates[0]
	cfg := clientConfig{CAPEM: string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: base.Certificate().Raw}))}
	base.Close()
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{cert}, NextProtos: []string{"h2"}})
	if err != nil {
		t.Fatal(err)
	}
	var active sync.Map
	count := new(atomic.Int64)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			c, e := ln.Accept()
			if e != nil {
				return
			}
			count.Add(1)
			active.Store(c, true)
			go func() {
				defer active.Delete(c)
				defer c.Close()
				c.SetDeadline(time.Now().Add(5 * time.Second))
				p := make([]byte, len(http2.ClientPreface))
				if _, e := io.ReadFull(c, p); e != nil {
					return
				}
				if string(p) != http2.ClientPreface {
					return
				}
				handler(c, http2.NewFramer(c, c))
			}()
		}
	}()
	t.Cleanup(func() { ln.Close(); active.Range(func(k, v any) bool { k.(net.Conn).Close(); return true }); <-done })
	return "https://" + ln.Addr().String(), cfg, count
}
func TestAuditLargeResponseAndRawHeaders(t *testing.T) {
	block := bytes.Repeat([]byte("abcdefgh12345678"), 1<<16)
	const blocks = 12
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Accept-Encoding") != "" {
			t.Error("automatic compression")
		}
		w.Header().Add("X-Dupe", "one")
		w.Header().Add("X-Dupe", "two")
		for i := 0; i < blocks; i++ {
			if _, e := w.Write(block); e != nil {
				return
			}
		}
	})
	s := requestFor(t, cfg, requestIn{Method: "GET", URL: url, Timeout: 4, ReadTimeout: 1})
	es, end := auditDrain(t, s)
	if end.Kind != "eof" {
		t.Fatal(end)
	}
	var all []byte
	dupes := 0
	for _, e := range es {
		all = append(all, e.Data...)
		for _, h := range e.Headers {
			if h[0] == "X-Dupe" {
				dupes++
			}
		}
	}
	if len(all) != len(block)*blocks || !bytes.Equal(all, bytes.Repeat(block, blocks)) || dupes != 2 {
		t.Fatalf("bytes=%d dupes=%d", len(all), dupes)
	}
}
func TestAuditHeaderWaitCancelAndTotal(t *testing.T) {
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() })
	for _, mode := range []string{"read", "total", "cancel"} {
		t.Run(mode, func(t *testing.T) {
			in := requestIn{Method: "GET", URL: url}
			if mode == "read" {
				in.ReadTimeout = .06
			}
			if mode == "total" {
				in.Timeout = .06
			}
			s := requestFor(t, cfg, in)
			nextKind(t, s, "uploaded")
			if mode == "cancel" {
				s.cancel()
			}
			_, e := auditDrain(t, s)
			want := mode + "_timeout"
			if mode == "cancel" {
				want = "cancelled"
			}
			if e.Code != want || !e.Dispatched {
				t.Fatal(e)
			}
		})
	}
}
func TestAuditBlockedUploadCancelAndTimeout(t *testing.T) {
	url, cfg, _ := auditRaw(t, func(c net.Conn, f *http2.Framer) {
		f.WriteSettings(http2.Setting{ID: http2.SettingInitialWindowSize, Val: 0})
		for {
			if _, e := f.ReadFrame(); e != nil {
				return
			}
		}
	})
	for _, mode := range []string{"cancel", "write", "total"} {
		t.Run(mode, func(t *testing.T) {
			in := requestIn{Method: "POST", URL: url, Body: make([]byte, 1<<20)}
			if mode == "write" {
				in.WriteTimeout = .06
			}
			if mode == "total" {
				in.Timeout = .06
			}
			s := requestFor(t, cfg, in)
			nextKind(t, s, "dispatch")
			if mode == "cancel" {
				time.Sleep(20 * time.Millisecond)
				s.cancel()
			}
			_, e := auditDrain(t, s)
			want := mode + "_timeout"
			if mode == "cancel" {
				want = "cancelled"
			}
			if e.Code != want || !e.Dispatched {
				t.Fatal(e)
			}
		})
	}
}
func TestAuditNoReplayResetGoAway(t *testing.T) {
	for _, mode := range []string{"refused", "goaway"} {
		t.Run(mode, func(t *testing.T) {
			var requests atomic.Int64
			url, cfg, count := auditRaw(t, func(c net.Conn, f *http2.Framer) {
				f.WriteSettings()
				for {
					fr, e := f.ReadFrame()
					if e != nil {
						return
					}
					if h, ok := fr.(*http2.HeadersFrame); ok {
						requests.Add(1)
						if mode == "refused" {
							f.WriteRSTStream(h.StreamID, http2.ErrCodeRefusedStream)
						} else {
							f.WriteGoAway(0, http2.ErrCodeNo, nil)
						}
						return
					}
				}
			})
			s := requestFor(t, cfg, requestIn{Method: "POST", URL: url, Body: []byte("x"), Timeout: 1})
			_, e := auditDrain(t, s)
			if e.Kind != "error" || !e.Dispatched || count.Load() != 1 || requests.Load() != 1 {
				t.Fatalf("%+v connections=%d requests=%d", e, count.Load(), requests.Load())
			}
		})
	}
}
func TestAuditDNScancel(t *testing.T) {
	old := net.DefaultResolver
	entered := make(chan struct{}, 1)
	net.DefaultResolver = &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, address string) (net.Conn, error) {
		select {
		case entered <- struct{}{}:
		default:
		}
		<-ctx.Done()
		return nil, ctx.Err()
	}}
	defer func() { net.DefaultResolver = old }()
	s := requestFor(t, clientConfig{}, requestIn{Method: "GET", URL: "https://audit-only.invalid/", ConnectTimeout: 1})
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("not resolving")
	}
	s.cancel()
	_, e := auditDrain(t, s)
	if e.Code != "cancelled" || e.Dispatched {
		t.Fatal(e)
	}
}
func TestAuditCertHostnameAndALPN(t *testing.T) {
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) { t.Error("bad hostname reached HTTP") })
	s := requestFor(t, cfg, requestIn{Method: "GET", URL: strings.Replace(url, "127.0.0.1", "wrong-host.invalid", 1), DialIPs: []string{"127.0.0.1"}, RequireResolved: true})
	_, e := auditDrain(t, s)
	if e.Code != "certificate" || e.Dispatched {
		t.Fatal(e)
	}
	base := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("h1 fallback") }))
	defer base.Close()
	cfg.CAPEM = string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: base.Certificate().Raw}))
	s = requestFor(t, cfg, requestIn{Method: "GET", URL: base.URL})
	_, e = auditDrain(t, s)
	if e.Dispatched || !strings.Contains(e.Message, "did not negotiate h2") {
		t.Fatal(e)
	}
}
func TestAuditSlowConsumerDoesNotReadTimeout(t *testing.T) {
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) { w.Write(bytes.Repeat([]byte("A"), 128<<10)) })
	s := requestFor(t, cfg, requestIn{Method: "GET", URL: url, ReadTimeout: .05, Timeout: 2})
	nextKind(t, s, "headers")
	nextKind(t, s, "data")
	time.Sleep(150 * time.Millisecond)
	_, e := auditDrain(t, s)
	if e.Kind != "eof" {
		t.Fatal(e)
	}
}
func TestAuditCloseRaces(t *testing.T) {
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) { w.(http.Flusher).Flush(); <-r.Context().Done() })
	for i := 0; i < 20; i++ {
		cid := newClient(cfg)
		var wg sync.WaitGroup
		for j := 0; j < 12; j++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				rid, e := startRequest(cid, requestIn{Method: "POST", URL: url, Body: []byte("x")})
				if e != nil {
					return
				}
				s := getRequest(rid)
				if s != nil {
					s.poll(time.Millisecond)
					s.cancel()
				}
				closeRequest(rid)
			}()
		}
		wg.Add(1)
		go func() { defer wg.Done(); time.Sleep(time.Millisecond); closeClient(cid) }()
		wg.Wait()
		closeClient(cid)
	}
	handles.Lock()
	defer handles.Unlock()
	if len(handles.clients) != 0 || len(handles.requests) != 0 {
		t.Fatalf("leaked %d/%d", len(handles.clients), len(handles.requests))
	}
}
func TestAuditOversizedProxyHeader(t *testing.T) {
	ln, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer ln.Close()
	written := make(chan error, 1)
	go func() {
		c, e := ln.Accept()
		if e != nil {
			written <- e
			return
		}
		defer c.Close()
		r := bufio.NewReader(c)
		if _, e = http.ReadRequest(r); e != nil {
			written <- e
			return
		}
		_, e = fmt.Fprintf(c, "HTTP/1.1 407 No\r\nX-Huge: %s\r\n\r\n", strings.Repeat("x", 16<<20))
		written <- e
	}()
	s := requestFor(t, clientConfig{Proxy: "http://" + ln.Addr().String()}, requestIn{Method: "GET", URL: "https://remote.invalid", ConnectTimeout: 2})
	_, end := auditDrain(t, s)
	t.Logf("16MiB CONNECT header terminal=%+v writer=%v", end, <-written)
	if end.Message != errConnectHeaderLimit.Error() || end.Dispatched {
		t.Fatal(end)
	}
}
func TestAuditEarlyResponseUploadedOrder(t *testing.T) {
	url, cfg, _ := auditRaw(t, func(c net.Conn, f *http2.Framer) {
		f.WriteSettings(http2.Setting{ID: http2.SettingInitialWindowSize, Val: 0})
		for {
			fr, e := f.ReadFrame()
			if e != nil {
				return
			}
			if h, ok := fr.(*http2.HeadersFrame); ok {
				var b bytes.Buffer
				enc := hpack.NewEncoder(&b)
				enc.WriteField(hpack.HeaderField{Name: ":status", Value: "413"})
				f.WriteHeaders(http2.HeadersFrameParam{StreamID: h.StreamID, BlockFragment: b.Bytes(), EndHeaders: true, EndStream: true})
				for {
					if _, e = f.ReadFrame(); e != nil {
						return
					}
				}
			}
		}
	})
	s := requestFor(t, cfg, requestIn{Method: "POST", URL: url, Body: make([]byte, 1<<20), Timeout: 1, WriteTimeout: .1})
	es, end := auditDrain(t, s)
	t.Logf("early response events=%+v terminal=%+v", es, end)
	if end.Kind != "eof" {
		t.Fatal(end)
	}
}
