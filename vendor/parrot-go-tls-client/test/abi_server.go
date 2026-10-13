// Loopback-only fixture for the real ctypes regression tests. No external DNS.
package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
)

func main() {
	var outputMu sync.Mutex
	allowBody := make(chan struct{})
	var releaseBody sync.Once
	emit := func(v any) { outputMu.Lock(); defer outputMu.Unlock(); json.NewEncoder(os.Stdout).Encode(v) }
	origin := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		emit(map[string]any{"path": r.URL.Path, "method": r.Method})
		switch r.URL.Path {
		case "/hold":
			<-r.Context().Done()
		case "/empty":
			w.WriteHeader(204)
		case "/echo":
			for _, v := range r.Header.Values("X-Raw") {
				w.Header().Add("X-Raw", v)
			}
			w.Header().Add("Set-Cookie", "a=caf\xe9")
			w.Header().Add("Set-Cookie", "b=\xff\x80")
			io.WriteString(w, "ok")
		case "/release-body":
			releaseBody.Do(func() { close(allowBody) })
			w.WriteHeader(204)
		case "/body-digest", "/body-gated":
			if r.URL.Path == "/body-gated" {
				select {
				case <-allowBody:
				case <-r.Context().Done():
					return
				}
			}
			hash := sha256.New()
			n, err := io.Copy(hash, r.Body)
			if err != nil {
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"length": n, "sha256": fmt.Sprintf("%x", hash.Sum(nil))})
		case "/large":
			io.CopyN(w, strings.NewReader(strings.Repeat("a", 2<<20)), 2<<20)
		default:
			io.WriteString(w, "ok")
		}
	}))
	origin.EnableHTTP2 = true
	origin.StartTLS()
	defer origin.Close()
	authority := strings.TrimPrefix(origin.URL, "https://")
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		emit(map[string]any{"proxy": true, "method": r.Method, "authenticated": r.Header.Get("Proxy-Authorization") == "Basic "+base64.StdEncoding.EncodeToString([]byte("fixture:secret"))})
		if r.Method != "CONNECT" || r.Host != authority || r.Header.Get("Proxy-Authorization") != "Basic "+base64.StdEncoding.EncodeToString([]byte("fixture:secret")) {
			w.WriteHeader(407)
			return
		}
		remote, err := net.Dial("tcp", authority)
		if err != nil {
			w.WriteHeader(502)
			return
		}
		defer remote.Close()
		conn, rw, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer conn.Close()
		io.WriteString(conn, "HTTP/1.1 200 Connection Established\r\n\r\n")
		done := make(chan struct{})
		go func() { io.Copy(remote, rw); remote.Close(); close(done) }()
		io.Copy(conn, remote)
		conn.Close()
		<-done
	}))
	defer proxy.Close()
	emit(map[string]any{"url": origin.URL, "proxy": proxy.URL, "ca": string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: origin.Certificate().Raw}))})
	bufio.NewReader(os.Stdin).ReadByte()
}
