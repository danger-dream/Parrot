package main

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"sync/atomic"
	"time"
)

// Handles are retained only until explicit Close. No process-wide connection cache.
type clientConfig struct {
	Proxy     string   `json:"proxy"`
	ProxyAuth []string `json:"proxy_auth"`
	Insecure  bool     `json:"insecure"`
	CAPEM     string   `json:"ca_pem"`
}
type clientState struct {
	config   clientConfig
	mu       sync.Mutex
	closed   bool
	requests map[uint64]*requestState
}
type requestIn struct {
	Method          string      `json:"method"`
	URL             string      `json:"url"`
	Headers         [][2]string `json:"headers"`     // Legacy UTF-8 string input.
	HeadersB64      [][2][]byte `json:"headers_b64"` // Authoritative byte pairs when present.
	Body            []byte      `json:"body_b64"`
	DialIPs         []string    `json:"dial_ips"` // IPs for the immediate socket peer, NOT a proxied target.
	RequireResolved bool        `json:"require_resolved"`
	Timeout         float64     `json:"timeout_s"`
	ConnectTimeout  float64     `json:"connect_timeout_s"`
	ReadTimeout     float64     `json:"read_timeout_s"`
	WriteTimeout    float64     `json:"write_timeout_s"`
}
type event struct {
	Kind            string      `json:"kind"`
	Status          int         `json:"status,omitempty"`
	Headers         [][2]string `json:"headers,omitempty"` // Legacy display-only strings.
	HeadersB64      [][2][]byte `json:"headers_b64"`
	Data            []byte      `json:"data,omitempty"`
	Code            string      `json:"code,omitempty"`
	Phase           string      `json:"phase,omitempty"`
	Message         string      `json:"message,omitempty"`
	Dispatched      bool        `json:"dispatched"`
	DispatchUnknown bool        `json:"dispatch_unknown,omitempty"`
	DurationMS      int64       `json:"duration_ms,omitempty"`
}
type requestState struct {
	id                uint64
	owner             *clientState
	ctx               context.Context
	cancel            context.CancelFunc
	events            chan event // One 32KiB body chunk + the active read; bounded backpressure.
	done              chan struct{}
	terminal          event // Read only after done is closed.
	dispatched        atomic.Bool
	uploaded          atomic.Bool
	wake              chan struct{} // Coalesced lifecycle notification, never consumer backpressure.
	pollMu            sync.Mutex
	dispatchDelivered bool
	uploadedDelivered bool
	pending           *event // At most one already-read bounded event.
	phase             atomic.Value
	timeoutPhase      atomic.Value
	started           time.Time
}

var handles = struct {
	sync.Mutex
	next     uint64
	clients  map[uint64]*clientState
	requests map[uint64]*requestState
}{clients: map[uint64]*clientState{}, requests: map[uint64]*requestState{}}

func newClient(config clientConfig) uint64 {
	handles.Lock()
	defer handles.Unlock()
	handles.next++
	handles.clients[handles.next] = &clientState{config: config, requests: map[uint64]*requestState{}}
	return handles.next
}
func startRequest(id uint64, in requestIn) (uint64, error) {
	handles.Lock()
	c := handles.clients[id]
	handles.Unlock()
	if c == nil {
		return 0, errors.New("client closed")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return 0, errors.New("client closed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	if in.Timeout > 0 {
		cancel()
		ctx, cancel = context.WithTimeout(context.Background(), seconds(in.Timeout))
	}
	s := &requestState{owner: c, ctx: ctx, cancel: cancel, events: make(chan event, 1), wake: make(chan struct{}, 1), done: make(chan struct{}), started: time.Now()}
	s.phase.Store("setup")
	s.timeoutPhase.Store("")
	handles.Lock()
	handles.next++
	s.id = handles.next
	handles.requests[s.id] = s
	handles.Unlock()
	c.requests[s.id] = s
	go s.run(in)
	return s.id, nil
}
func seconds(s float64) time.Duration { return time.Duration(s * float64(time.Second)) }
func getRequest(id uint64) *requestState {
	handles.Lock()
	defer handles.Unlock()
	return handles.requests[id]
}
func (s *requestState) emit(e event) bool {
	e.Dispatched = s.dispatched.Load()
	select {
	case s.events <- e:
		return true
	case <-s.ctx.Done():
		return false
	}
}
func (s *requestState) notifyLifecycle() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}
func (s *requestState) poll(wait time.Duration) event {
	s.pollMu.Lock()
	defer s.pollMu.Unlock()
	timer := time.NewTimer(wait)
	defer timer.Stop()
	for {
		// Lifecycle facts have fixed cardinality and do not occupy the data slot.
		// Check again after receiving a data event to preserve dispatch ordering.
		if s.dispatched.Load() && !s.dispatchDelivered {
			s.dispatchDelivered = true
			return event{Kind: "dispatch", Dispatched: true}
		}
		if s.uploaded.Load() && !s.uploadedDelivered {
			s.uploadedDelivered = true
			return event{Kind: "uploaded", Dispatched: true}
		}
		if s.pending != nil {
			e := *s.pending
			s.pending = nil
			return e
		}
		// Prefer already-available events over the zero-wait timer.
		select {
		case e := <-s.events:
			s.pending = &e
			continue
		default:
		}
		select {
		case e := <-s.events:
			s.pending = &e
		case <-s.wake:
		case <-s.done:
			select {
			case e := <-s.events:
				s.pending = &e
			default:
				e := s.terminal
				s.pending = &e
			}
		case <-timer.C:
			return event{Kind: "pending", Dispatched: s.dispatched.Load()}
		}
	}
}
func closeRequest(id uint64) {
	s := getRequest(id)
	if s == nil {
		return
	}
	s.cancel()
	<-s.done
	s.owner.mu.Lock()
	delete(s.owner.requests, id)
	s.owner.mu.Unlock()
	handles.Lock()
	delete(handles.requests, id)
	handles.Unlock()
}
func closeClient(id uint64) {
	handles.Lock()
	c := handles.clients[id]
	delete(handles.clients, id)
	handles.Unlock()
	if c == nil {
		return
	}
	c.mu.Lock()
	c.closed = true
	ids := make([]uint64, 0, len(c.requests))
	for id, s := range c.requests {
		s.cancel()
		ids = append(ids, id)
	}
	c.mu.Unlock()
	for _, id := range ids {
		closeRequest(id)
	}
}
func marshal(v any) []byte { b, _ := json.Marshal(v); return b }
