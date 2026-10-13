package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"strings"
	"sync"
	"time"

	tlsu "github.com/refraction-networking/utls"
	"golang.org/x/net/http2"
	"golang.org/x/net/proxy"
)

// A connection is owned by exactly one request. ClientConn.RoundTrip deliberately
// avoids Transport.RoundTrip's transparent h2 retries. No redirect or replay,
// cross-request pool, or automatic response decompression.
func (s *requestState) run(in requestIn) {
	var finalErr error
	defer func() {
		if recover() != nil {
			finalErr = errors.New("native transport panic")
		}
		if finalErr == nil && s.ctx.Err() != nil {
			finalErr = s.ctx.Err()
		}
		s.terminal = event{Kind: "eof", Dispatched: s.dispatched.Load(), DurationMS: time.Since(s.started).Milliseconds()}
		if finalErr != nil {
			s.terminal = s.failure(finalErr)
		}
		s.cancel()
		close(s.done)
	}()
	req, err := http.NewRequestWithContext(s.ctx, strings.ToUpper(in.Method), in.URL, bytes.NewReader(in.Body))
	if err != nil {
		finalErr = errors.New("invalid request URL or method")
		return
	}
	if req.URL.Scheme != "https" || req.URL.Hostname() == "" || req.URL.User != nil {
		finalErr = errors.New("only HTTPS targets without URL credentials are supported")
		return
	}
	headersIn := in.Headers
	if in.HeadersB64 != nil {
		headersIn = make([][2]string, 0, len(in.HeadersB64))
		for _, h := range in.HeadersB64 {
			headersIn = append(headersIn, [2]string{string(h[0]), string(h[1])})
		}
	}
	for _, h := range headersIn {
		if strings.EqualFold(h[0], "host") {
			req.Host = h[1]
		} else {
			req.Header.Add(h[0], h[1])
		}
	}
	connectTimeout := in.ConnectTimeout
	if connectTimeout == 0 {
		connectTimeout = 15
	}
	var connectCtx context.Context
	var cancelConnect context.CancelFunc
	if connectTimeout < 0 {
		connectCtx, cancelConnect = context.WithCancel(s.ctx)
	} else {
		connectCtx, cancelConnect = context.WithTimeout(s.ctx, seconds(connectTimeout))
	}
	s.phase.Store("connect")
	conn, err := dialLS(connectCtx, s.owner.config, req.URL, in)
	if err != nil {
		if connectCtx.Err() == context.DeadlineExceeded && s.ctx.Err() == nil {
			s.timeoutPhase.Store("connect")
		}
		cancelConnect()
		finalErr = err
		return
	}
	cancelConnect()
	defer conn.Close()
	stopClose := context.AfterFunc(s.ctx, func() { conn.Close() })
	defer stopClose()
	transport := &http2.Transport{MaxReadFrameSize: 1048576, DisableCompression: true}
	cc, err := transport.NewClientConn(conn)
	if err != nil {
		finalErr = err
		return
	}
	defer cc.Close()
	s.phase.Store("write")
	var uploadTimer *time.Timer
	if in.WriteTimeout > 0 {
		uploadTimer = time.AfterFunc(seconds(in.WriteTimeout), func() { s.timeoutPhase.Store("write"); s.cancel() })
		defer uploadTimer.Stop()
	}
	var headerMu sync.Mutex
	headersDone := false
	stopHeader := func() {}
	finishHeaderWait := func() {
		headerMu.Lock()
		headersDone = true
		stopHeader()
		headerMu.Unlock()
	}
	defer finishHeaderWait()
	trace := &httptrace.ClientTrace{WroteRequest: func(info httptrace.WroteRequestInfo) {
		if uploadTimer != nil {
			uploadTimer.Stop()
		}
		if info.Err == nil {
			s.phase.Store("read")
			headerMu.Lock()
			if !headersDone {
				stopHeader = s.readGuard(in.ReadTimeout)
			}
			headerMu.Unlock()
			s.uploaded.Store(true)
			s.notifyLifecycle()
		}
	}}
	req = req.WithContext(httptrace.WithClientTrace(req.Context(), trace))
	// Conservative boundary: after TLS/preface but before request HEADERS may
	// leave. Later errors must never be advertised as safe for route failover.
	s.dispatched.Store(true)
	s.notifyLifecycle()
	resp, err := cc.RoundTrip(req)
	finishHeaderWait()
	if err != nil {
		finalErr = err
		return
	}
	defer resp.Body.Close()
	s.phase.Store("read")
	headers := make([][2]string, 0, len(resp.Header))
	headersB64 := make([][2][]byte, 0, len(resp.Header))
	for k, values := range resp.Header {
		for _, v := range values {
			headers = append(headers, [2]string{k, v})
			headersB64 = append(headersB64, [2][]byte{[]byte(k), []byte(v)})
		}
	}
	if !s.emit(event{Kind: "headers", Status: resp.StatusCode, Headers: headers, HeadersB64: headersB64}) {
		finalErr = s.ctx.Err()
		return
	}
	buffer := make([]byte, 32*1024)
	for {
		stopRead := s.readGuard(in.ReadTimeout)
		n, err := resp.Body.Read(buffer)
		stopRead()
		if n > 0 && !s.emit(event{Kind: "data", Data: append([]byte(nil), buffer[:n]...)}) {
			finalErr = s.ctx.Err()
			return
		}
		if err == io.EOF {
			return
		}
		if err != nil {
			finalErr = err
			return
		}
	}
}
func (s *requestState) readGuard(timeout float64) func() {
	if timeout <= 0 {
		return func() {}
	}
	timer := time.AfterFunc(seconds(timeout), func() { s.timeoutPhase.Store("read"); s.cancel() })
	return func() { timer.Stop() }
}
func (s *requestState) failure(err error) event {
	phase := s.phase.Load().(string)
	code := "protocol"
	if phase == "setup" {
		code = "invalid_request"
	} else if !s.dispatched.Load() {
		code = "connect"
	} else if phase == "read" {
		code = "read"
	}
	if timed := s.timeoutPhase.Load().(string); timed != "" {
		code = timed + "_timeout"
		phase = timed
	} else if errors.Is(s.ctx.Err(), context.DeadlineExceeded) {
		code = "total_timeout"
	} else if errors.Is(s.ctx.Err(), context.Canceled) {
		code = "cancelled"
	} else {
		var ne net.Error
		if errors.As(err, &ne) && ne.Timeout() {
			code = phase + "_timeout"
		}
		var certErr *tlsu.CertificateVerificationError
		var proxyCertErr *tls.CertificateVerificationError
		if errors.As(err, &certErr) || errors.As(err, &proxyCertErr) {
			code = "certificate"
		}
	}
	return event{Kind: "error", Code: code, Phase: phase, Message: err.Error(), Dispatched: s.dispatched.Load(), DurationMS: time.Since(s.started).Milliseconds()}
}

type resolvedDialer struct {
	ips      []string
	required bool
}

func (d *resolvedDialer) Dial(network, address string) (net.Conn, error) {
	return d.DialContext(context.Background(), network, address)
}
func (d *resolvedDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}
	addresses := d.ips
	if len(addresses) == 0 {
		if d.required && net.ParseIP(host) == nil {
			return nil, errors.New("caller must resolve immediate peer using its DNS policy")
		}
		addresses = []string{host}
	}
	var last error
	for _, ip := range addresses {
		if len(d.ips) > 0 && net.ParseIP(ip) == nil {
			return nil, errors.New("dial_ips must contain IP literals")
		}
		conn, err := (&net.Dialer{KeepAlive: 30 * time.Second}).DialContext(ctx, network, net.JoinHostPort(ip, port))
		if err == nil {
			return conn, nil
		}
		last = err
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	return nil, last
}

type bufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *bufferedConn) Read(p []byte) (int, error) { return c.reader.Read(p) }
func rootPool(pem string) (*x509.CertPool, error) {
	if pem == "" {
		return nil, nil
	}
	roots, err := x509.SystemCertPool()
	if err != nil {
		roots = x509.NewCertPool()
	}
	if !roots.AppendCertsFromPEM([]byte(pem)) {
		return nil, errors.New("invalid CA PEM")
	}
	return roots, nil
}
func dialLS(ctx context.Context, cfg clientConfig, target *url.URL, in requestIn) (net.Conn, error) {
	roots, err := rootPool(cfg.CAPEM)
	if err != nil {
		return nil, err
	}
	port := target.Port()
	if port == "" {
		port = "443"
	}
	address := net.JoinHostPort(target.Hostname(), port)
	forward := &resolvedDialer{ips: in.DialIPs, required: in.RequireResolved}
	var conn net.Conn
	if cfg.Proxy == "" {
		conn, err = forward.DialContext(ctx, "tcp", address)
	} else {
		pu, parseErr := url.Parse(cfg.Proxy)
		if parseErr != nil || pu.Hostname() == "" {
			return nil, errors.New("invalid proxy URL")
		}
		switch pu.Scheme {
		case "socks5", "socks5h":
			if len(cfg.ProxyAuth) == 2 {
				pu.User = url.UserPassword(cfg.ProxyAuth[0], cfg.ProxyAuth[1])
			}
			pd, e := proxy.FromURL(pu, forward)
			if e != nil {
				return nil, e
			}
			conn, err = pd.(proxy.ContextDialer).DialContext(ctx, "tcp", address)
		case "http", "https":
			pp := pu.Port()
			if pp == "" {
				pp = "80"
				if pu.Scheme == "https" {
					pp = "443"
				}
			}
			conn, err = forward.DialContext(ctx, "tcp", net.JoinHostPort(pu.Hostname(), pp))
			if err != nil {
				return nil, err
			}
			raw := conn
			stop := context.AfterFunc(ctx, func() { raw.Close() })
			if deadline, ok := ctx.Deadline(); ok {
				raw.SetDeadline(deadline)
			}
			if pu.Scheme == "https" {
				pc := tls.Client(conn, &tls.Config{ServerName: pu.Hostname(), RootCAs: roots, MinVersion: tls.VersionTLS12})
				err = pc.HandshakeContext(ctx)
				conn = pc
			}
			if err == nil {
				request := &http.Request{Method: http.MethodConnect, URL: &url.URL{Opaque: address}, Host: address, Header: http.Header{}}
				auth := cfg.ProxyAuth
				if len(auth) == 0 && pu.User != nil {
					password, _ := pu.User.Password()
					auth = []string{pu.User.Username(), password}
				}
				if len(auth) == 2 {
					request.Header.Set("Proxy-Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte(auth[0]+":"+auth[1])))
				}
				err = request.Write(conn)
				if err == nil {
					reader := bufio.NewReader(conn)
					var response *http.Response
					response, err = readConnectResponse(reader, request)
					if err == nil && response.StatusCode != 200 {
						err = fmt.Errorf("proxy CONNECT status %d", response.StatusCode)
					}
					if err == nil {
						conn = &bufferedConn{Conn: conn, reader: reader}
					}
				}
			}
			stop()
			if err != nil {
				raw.Close()
				return nil, err
			}
		default:
			return nil, errors.New("unsupported proxy scheme")
		}
	}
	if err != nil {
		return nil, err
	}
	raw := conn
	stop := context.AfterFunc(ctx, func() { raw.Close() })
	defer stop()
	if deadline, ok := ctx.Deadline(); ok {
		raw.SetDeadline(deadline)
	}
	uc := tlsu.UClient(conn, &tlsu.Config{ServerName: target.Hostname(), RootCAs: roots, InsecureSkipVerify: cfg.Insecure}, tlsu.HelloCustom)
	if err = uc.ApplyPreset(lsClientHelloSpec()); err == nil {
		err = uc.HandshakeContext(ctx)
	}
	if err == nil && uc.ConnectionState().NegotiatedProtocol != "h2" {
		err = errors.New("upstream did not negotiate h2; HTTP/1.1 fallback is not implemented")
	}
	if err == nil {
		err = ctx.Err()
	}
	if err != nil {
		raw.Close()
		return nil, err
	}
	uc.SetDeadline(time.Time{})
	return uc, nil
}
