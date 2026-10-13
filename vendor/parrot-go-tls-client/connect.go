package main

import (
	"bufio"
	"bytes"
	"errors"
	"net/http"
)

const maxConnectHeaderBytes = 64 << 10
const maxConnectHeaderLines = 100 // Continuations count too; excludes status/empty terminator.

var errConnectHeaderLimit = errors.New("proxy CONNECT response header limit exceeded")

// Limit untrusted status+headers BEFORE net/http's otherwise unbounded MIME
// parsing. The original reader remains on the tunnel, including prefetched TLS
// bytes; only the bounded header copy is passed to ReadResponse. No limiter can
// accidentally truncate a successful TLS tunnel.
func readConnectResponse(reader *bufio.Reader, request *http.Request) (*http.Response, error) {
	var raw bytes.Buffer
	lines := 0
	for {
		lineBytes := 0
		var first byte
		for {
			fragment, err := reader.ReadSlice('\n')
			if raw.Len()+len(fragment) > maxConnectHeaderBytes {
				return nil, errConnectHeaderLimit
			}
			if lineBytes == 0 && len(fragment) > 0 {
				first = fragment[0]
			}
			lineBytes += len(fragment)
			raw.Write(fragment)
			if err == bufio.ErrBufferFull {
				continue
			}
			if err != nil {
				return nil, err
			}
			break
		}
		if lines > 0 && (lineBytes == 1 && first == '\n' || lineBytes == 2 && first == '\r') {
			break
		}
		// First physical line is the status; every further physical line is bounded.
		if lines > maxConnectHeaderLines {
			return nil, errConnectHeaderLimit
		}
		lines++
	}
	return http.ReadResponse(bufio.NewReader(bytes.NewReader(raw.Bytes())), request)
}
