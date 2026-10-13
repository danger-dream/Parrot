package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestCompletedEmptyResponseIgnoresConsumerBackpressure(t *testing.T) {
	for _, method := range []string{"GET", "HEAD"} {
		for _, status := range []int{204, 302} {
			received := make(chan struct{})
			target, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status); close(received) })
			s := requestFor(t, cfg, requestIn{Method: method, URL: target, ReadTimeout: .05})
			<-received
			time.Sleep(150 * time.Millisecond)
			es, end := auditDrain(t, s)
			if end.Kind != "eof" {
				t.Fatalf("%s/%d: %+v events=%+v", method, status, end, es)
			}
			found := false
			for _, e := range es {
				if e.Kind == "headers" && e.Status == status {
					found = true
				}
			}
			if !found {
				t.Fatalf("missing successful headers: %+v", es)
			}
		}
	}
}

func TestConnectHeaderBoundsAndBufferedTunnel(t *testing.T) {
	request := &http.Request{Method: "CONNECT"}
	for name, raw := range map[string]string{
		"long-line":     "HTTP/1.1 200 OK\r\nX: " + strings.Repeat("x", maxConnectHeaderBytes),
		"long-status":   "HTTP/1.1 200 " + strings.Repeat("x", maxConnectHeaderBytes),
		"fields":        "HTTP/1.1 200 OK\r\n" + strings.Repeat("X: a\r\n", maxConnectHeaderLines+1) + "\r\n",
		"continuations": "HTTP/1.1 200 OK\r\nX: a\r\n" + strings.Repeat(" a\r\n", maxConnectHeaderLines) + "\r\n",
	} {
		t.Run(name, func(t *testing.T) {
			_, err := readConnectResponse(bufio.NewReader(strings.NewReader(raw)), request)
			if !errors.Is(err, errConnectHeaderLimit) {
				t.Fatal(err)
			}
		})
	}
	head := "HTTP/1.1 200 OK\r\n" + strings.Repeat("X: a\r\n", maxConnectHeaderLines) + "\r\n"
	tunnel := bytes.Repeat([]byte{0, 0xff, 0x16, 0x80}, 10000)
	reader := bufio.NewReader(io.MultiReader(strings.NewReader(head), bytes.NewReader(tunnel)))
	response, err := readConnectResponse(reader, request)
	if err != nil || response.StatusCode != 200 {
		t.Fatalf("%v %+v", err, response)
	}
	actual, err := io.ReadAll(reader)
	if err != nil || !bytes.Equal(actual, tunnel) {
		t.Fatal("CONNECT parser lost or limited tunnel bytes", err)
	}
}

func TestCancelIncompleteConnectHeader(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	sent := make(chan struct{})
	ended := make(chan struct{})
	go func() {
		defer close(ended)
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		if _, err = http.ReadRequest(bufio.NewReader(conn)); err != nil {
			return
		}
		io.WriteString(conn, "HTTP/1.1 200 OK\r\nX: incomplete")
		close(sent)
		io.Copy(io.Discard, conn)
	}()
	s := requestFor(t, clientConfig{Proxy: "http://" + ln.Addr().String()}, requestIn{Method: "GET", URL: "https://remote.invalid/"})
	<-sent
	s.cancel()
	_, e := auditDrain(t, s)
	if e.Code != "cancelled" || e.Dispatched {
		t.Fatal(e)
	}
	select {
	case <-ended:
	case <-time.After(time.Second):
		t.Fatal("incomplete CONNECT socket leaked")
	}
}

func TestRawHeaderBytesThroughJSONAndH2(t *testing.T) {
	values := []string{"caf\xe9", "\xff\x80", "utf8\xc3\xa9"}
	received := make(chan []string, 1)
	target, cfg := localServer(t, func(w http.ResponseWriter, r *http.Request) {
		received <- r.Header.Values("X-Raw")
		for _, v := range values {
			w.Header().Add("X-Raw", v)
		}
		w.WriteHeader(204)
	})
	in := requestIn{Method: "GET", URL: target, Headers: [][2]string{{"X-Raw", "ignored-legacy"}}, HeadersB64: [][2][]byte{}}
	for _, v := range values {
		in.HeadersB64 = append(in.HeadersB64, [2][]byte{[]byte("X-Raw"), []byte(v)})
	}
	wire, err := json.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	var decoded requestIn
	if err = json.Unmarshal(wire, &decoded); err != nil {
		t.Fatal(err)
	}
	s := requestFor(t, cfg, decoded)
	es, end := auditDrain(t, s)
	if end.Kind != "eof" {
		t.Fatal(end)
	}
	got := <-received
	if strings.Join(got, "|") != strings.Join(values, "|") {
		t.Fatalf("request bytes changed: %q", got)
	}
	var response []string
	for _, e := range es {
		if e.Kind == "headers" {
			var out event
			if err = json.Unmarshal(marshal(e), &out); err != nil {
				t.Fatal(err)
			}
			for _, h := range out.HeadersB64 {
				if strings.EqualFold(string(h[0]), "x-raw") {
					response = append(response, string(h[1]))
				}
			}
		}
	}
	if strings.Join(response, "|") != strings.Join(values, "|") {
		t.Fatalf("response bytes changed: %q", response)
	}
}

func TestUnknownNativeHandleCannotAssertUnsent(t *testing.T) {
	e := closedRequestEvent()
	if !e.Dispatched || !e.DispatchUnknown {
		t.Fatal(e)
	}
	// No per-handle tombstone is created by this conservative response.
}
