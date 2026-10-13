package main

import (
	"bufio"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestAuditAuthenticatedProxiesAndRemoteDNS(t *testing.T) {
	for _, scheme := range []string{"http", "https", "socks5", "socks5h"} {
		t.Run(scheme, func(t *testing.T) {
			origin := make(chan [2]string, 1)
			target, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) {
				origin <- [2]string{r.Header.Get("Authorization"), r.Header.Get("Proxy-Authorization")}
				io.WriteString(w, "ok")
			})
			u, _ := url.Parse(target)
			hostport := u.Host
			base := httptest.NewUnstartedServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
			base.StartTLS()
			cert := base.TLS.Certificates[0]
			base.Close()
			ln, e := net.Listen("tcp", "127.0.0.1:0")
			if e != nil {
				t.Fatal(e)
			}
			defer ln.Close()
			if scheme == "https" {
				ln = tls.NewListener(ln, &tls.Config{Certificates: []tls.Certificate{cert}})
			}
			capture := make(chan string, 1)
			done := make(chan error, 1)
			go func() {
				var err error
				defer func() { done <- err }()
				c, e := ln.Accept()
				if e != nil {
					err = e
					return
				}
				defer c.Close()
				c.SetDeadline(time.Now().Add(3 * time.Second))
				var reader io.Reader = c
				if scheme == "http" || scheme == "https" {
					br := bufio.NewReader(c)
					reader = br
					req, e := http.ReadRequest(br)
					if e != nil {
						err = e
						return
					}
					if req.Method != "CONNECT" || req.Header.Get("Proxy-Authorization") != "Basic "+base64.StdEncoding.EncodeToString([]byte("user:pa:ss")) {
						err = fmt.Errorf("CONNECT auth/method mismatch")
						return
					}
					capture <- req.Host
					fmt.Fprint(c, "HTTP/1.1 200 Connection Established\r\n\r\n")
				} else {
					g := make([]byte, 2)
					if _, e := io.ReadFull(c, g); e != nil {
						err = e
						return
					}
					methods := make([]byte, int(g[1]))
					io.ReadFull(c, methods)
					c.Write([]byte{5, 2})
					io.ReadFull(c, g)
					name := make([]byte, int(g[1]))
					io.ReadFull(c, name)
					io.ReadFull(c, g[:1])
					pass := make([]byte, int(g[0]))
					io.ReadFull(c, pass)
					if string(name) != "user" || string(pass) != "pa:ss" {
						err = fmt.Errorf("SOCKS auth mismatch")
						return
					}
					c.Write([]byte{1, 0})
					hdr := make([]byte, 5)
					io.ReadFull(c, hdr)
					if hdr[3] != 3 {
						err = fmt.Errorf("expected remote FQDN, got %d", hdr[3])
						return
					}
					name = make([]byte, int(hdr[4]))
					io.ReadFull(c, name)
					io.ReadFull(c, g)
					capture <- net.JoinHostPort(string(name), fmt.Sprint(binary.BigEndian.Uint16(g)))
					c.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 1})
				}
				upstream, e := net.Dial("tcp", hostport)
				if e != nil {
					err = e
					return
				}
				defer upstream.Close()
				copyDone := make(chan struct{})
				go func() { io.Copy(upstream, reader); upstream.Close(); close(copyDone) }()
				io.Copy(c, upstream)
				c.Close()
				<-copyDone
			}()
			// Proxy is deliberately a DNS name not usable locally; ONLY the supplied immediate-peer IP can work.
			_, port, _ := net.SplitHostPort(ln.Addr().String())
			cfg.Proxy = scheme + "://example.com:" + port
			cfg.ProxyAuth = []string{"user", "pa:ss"}
			// target certificate is valid for example.com; neither target nor proxy name is resolved here.
			in := requestIn{Method: "POST", URL: "https://example.com:" + u.Port() + "/", Body: []byte("x"), Headers: [][2]string{{"Authorization", "Bearer local-fixture"}}, DialIPs: []string{"127.0.0.1"}, RequireResolved: true, Timeout: 2}
			s := requestFor(t, cfg, in)
			_, end := auditDrain(t, s)
			if end.Kind != "eof" {
				t.Fatal(end)
			}
			if got := <-capture; got != "example.com:"+u.Port() {
				t.Fatalf("authority %s", got)
			}
			if e := <-done; e != nil {
				t.Fatal(e)
			}
			auth := <-origin
			if auth[0] != "Bearer local-fixture" || auth[1] != "" {
				t.Fatalf("origin auth=%q proxy-auth=%q", auth[0], auth[1])
			}
		})
	}
}
func TestAuditHTTPSProxyCertificateNotDisabledByTargetInsecure(t *testing.T) {
	proxy := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("untrusted HTTPS proxy reached") }))
	defer proxy.Close()
	s := requestFor(t, clientConfig{Proxy: proxy.URL, Insecure: true}, requestIn{Method: "GET", URL: "https://example.com", ConnectTimeout: 1})
	_, e := auditDrain(t, s)
	if e.Dispatched || e.Code != "certificate" || !strings.Contains(e.Message, "certificate signed by unknown authority") {
		t.Fatal(e)
	}
	t.Logf("untrusted HTTPS proxy rejected and classified: %+v", e)
}
func TestAuditInvalidProxyNeverDirect(t *testing.T) {
	target, _ := localServer(t, func(w http.ResponseWriter, r *http.Request) { t.Error("bad proxy used DIRECT") })
	for _, proxy := range []string{"http://127.0.0.1:1", "unsupported://127.0.0.1", "not-a-url"} {
		s := requestFor(t, clientConfig{Proxy: proxy}, requestIn{Method: "GET", URL: target, Timeout: 1})
		_, e := auditDrain(t, s)
		if e.Dispatched || e.Kind != "error" {
			t.Fatal(e)
		}
	}
}
