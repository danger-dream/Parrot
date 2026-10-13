package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"encoding/json"
	"runtime"
	"time"
	"unsafe"
)

func output(v any) *C.char { return C.CString(string(marshal(v))) }
func inputError(message string) *C.char {
	return output(event{Kind: "error", Code: "invalid_request", Phase: "setup", Message: message})
}

// sourceID is injected by the controlled artifact build with -X main.sourceID=...
// A local developer build must not masquerade as a verified source snapshot.
var sourceID = "development"

//export ParrotBuildInfo
func ParrotBuildInfo() *C.char {
	return output(map[string]any{"version": "0.2.1", "abi": 3,
		"profile": "antigravity-ls-2.22.0", "go_version": runtime.Version(), "source_id": sourceID})
}

func closedRequestEvent() event {
	// No tombstones: a removed/unknown handle cannot establish that nothing was
	// sent. Dispatched is the conservative replay guard, not proof of delivery.
	return event{Kind: "error", Code: "invalid_request", Phase: "setup", Message: "request closed or unknown", Dispatched: true, DispatchUnknown: true}
}

//export ParrotNewClient
func ParrotNewClient(raw *C.char) *C.char {
	var cfg clientConfig
	if json.Unmarshal([]byte(C.GoString(raw)), &cfg) != nil {
		return inputError("invalid client JSON")
	}
	return output(map[string]uint64{"id": newClient(cfg)})
}

//export ParrotStart
func ParrotStart(client C.ulonglong, raw *C.char) *C.char {
	var in requestIn
	if json.Unmarshal([]byte(C.GoString(raw)), &in) != nil {
		return inputError("invalid request JSON")
	}
	id, err := startRequest(uint64(client), in)
	if err != nil {
		return inputError(err.Error())
	}
	return output(map[string]uint64{"id": id})
}

//export ParrotStartRaw
func ParrotStartRaw(client C.ulonglong, metadata *C.char, body unsafe.Pointer, bodyLen C.size_t) *C.char {
	var in requestIn
	if json.Unmarshal([]byte(C.GoString(metadata)), &in) != nil {
		return inputError("invalid request metadata JSON")
	}
	if in.Body != nil {
		return inputError("raw start metadata must not contain body_b64")
	}
	if uint64(bodyLen) > uint64(^uint(0)>>1) || body == nil && bodyLen != 0 {
		return inputError("invalid raw body pointer or length")
	}
	// The pointer is borrowed only for this call. Copy into Go-owned memory
	// before starting any goroutine or returning to Python. ctypes.CDLL releases
	// the GIL for this allocation/copy; there is no body JSON/base64 processing.
	in.Body = append([]byte(nil), unsafe.Slice((*byte)(body), int(bodyLen))...)
	id, err := startRequest(uint64(client), in)
	if err != nil {
		return inputError(err.Error())
	}
	return output(map[string]uint64{"id": id})
}

//export ParrotPoll
func ParrotPoll(id C.ulonglong, waitMS C.int) *C.char {
	s := getRequest(uint64(id))
	if s == nil {
		return output(closedRequestEvent())
	}
	ms := int(waitMS)
	if ms < 0 {
		ms = 0
	}
	if ms > 1000 {
		ms = 1000
	}
	return output(s.poll(time.Duration(ms) * time.Millisecond))
}

//export ParrotCancel
func ParrotCancel(id C.ulonglong) {
	if s := getRequest(uint64(id)); s != nil {
		s.cancel()
	}
}

//export ParrotCloseRequest
func ParrotCloseRequest(id C.ulonglong) *C.char {
	s := getRequest(uint64(id))
	if s == nil {
		return output(closedRequestEvent())
	}
	closeRequest(uint64(id))
	return output(s.terminal)
}

//export ParrotCloseClient
func ParrotCloseClient(id C.ulonglong) { closeClient(uint64(id)) }

//export ParrotFree
func ParrotFree(s *C.char) {
	if s != nil {
		C.free(unsafe.Pointer(s))
	}
}

func main() {}
