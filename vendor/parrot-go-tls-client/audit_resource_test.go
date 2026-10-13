package main

import (
	"context"
	"errors"
	"net"
	"net/http"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func TestAuditBackpressureWindowBound(t *testing.T) {
	var sent atomic.Int64
	ended := make(chan struct{})
	url, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) {
		defer close(ended)
		block := make([]byte, 32768)
		for i := 0; i < 2048; i++ {
			n, e := w.Write(block)
			sent.Add(int64(n))
			if e != nil {
				return
			}
			w.(http.Flusher).Flush()
		}
	})
	s := requestFor(t, cfg, requestIn{Method: "GET", URL: url, Timeout: 3})
	time.Sleep(200 * time.Millisecond) // never poll, offer 64MiB from constant-sized server buffer
	n := sent.Load()
	t.Logf("server progressed %d bytes under unconsumed response (stream window 4MiB)", n)
	if n == 0 || n > 5<<20 {
		t.Fatalf("unexpected backpressure amount %d", n)
	}
	s.cancel()
	closeRequest(s.id)
	select {
	case <-ended:
	case <-time.After(time.Second):
		t.Fatal("cancel did not reclaim producer")
	}
}
func TestAuditCancelBlockedTCP(t *testing.T) {
	ln, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer ln.Close()
	raw, e := ln.(*net.TCPListener).SyscallConn()
	if e != nil {
		t.Fatal(e)
	}
	var listenErr error
	raw.Control(func(fd uintptr) { listenErr = syscall.Listen(int(fd), 0) })
	if listenErr != nil {
		t.Fatal(listenErr)
	}
	// First local connection fills the zero-backlog fixture; do not accept it.
	first, e := net.DialTimeout("tcp", ln.Addr().String(), time.Second)
	if e != nil {
		t.Fatal(e)
	}
	defer first.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		c, e := (&resolvedDialer{}).DialContext(ctx, "tcp", ln.Addr().String())
		if c != nil {
			c.Close()
		}
		done <- e
	}()
	select {
	case e := <-done:
		t.Fatalf("TCP fixture did not block: %v", e)
	case <-time.After(40 * time.Millisecond):
	}
	start := time.Now()
	cancel()
	select {
	case e := <-done:
		if !errors.Is(e, context.Canceled) {
			t.Fatal(e)
		}
		t.Logf("blocked TCP cancelled in %v", time.Since(start))
	case <-time.After(time.Second):
		t.Fatal("TCP cancellation stuck")
	}
}
