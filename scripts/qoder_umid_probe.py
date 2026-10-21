#!/usr/bin/env python3
"""One generated-profile UMID probe. DRY RUN unless --execute --direct.

No config/account DB/SDK imports. Does not read credentials or sample hardware.
The caller must obtain approval for the destination and public egress first.
For an approved proxy, call probe(open_stream=connector.open_sync_stream) from
an isolated launcher with an explicitly supplied connector (no route fallback).
Never print/retain the issued ticket, blocks, request or response body.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import socket
import sys
import time
import types


def modules():
    root = Path(__file__).resolve().parents[1] / 'src' / 'oauth' / 'qoder'
    name = '_qoder_probe_codec'
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(root)]
    sys.modules[name] = pkg
    result = {}
    for leaf in ('device_blocks', 'device_profile', 'device_marks', 'umid_protocol', 'umid_http'):
        spec = importlib.util.spec_from_file_location(name + '.' + leaf, root / (leaf + '.py'))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        result[leaf] = mod
    return result


def probe(*, execute=False, open_stream=None):
    m = modules()
    profile = m['device_profile'].generate()
    blocks = m['device_profile'].validate(profile)
    summary = {'executed': False, 'profile_version': profile['version'],
               'field_counts': [len(v) for v in m['device_blocks'].decode_blocks(blocks)],
               'destination': 'pum.m.alibaba.com:443', 'transport': 'plaintext HTTP',
               'path': m['umid_protocol'].PATH, 'max_requests': 1}
    if not execute:
        return summary
    if open_stream is None:
        raise ValueError('An explicitly approved connector is required')
    deadline = time.monotonic() + 15
    stream = open_stream('pum.m.alibaba.com', 443, timeout=15)
    try:
        wire = m['umid_protocol'].request_bytes('pum.m.alibaba.com', m['umid_protocol'].build_payload(blocks))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        stream.settimeout(remaining)
        stream.sendall(wire)  # Exactly one send, never retry.
        raw = m['umid_http'].read_response(stream, deadline)
        token = m['umid_protocol'].parse_response(raw)
        marks = m['device_marks'].missing_detection(token)
        summary.update(executed=True, issued=True, ticket_length=len(token), marks_format=marks['format_version'])
        return summary
    finally:
        stream.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Send once; requires prior controller approval')
    parser.add_argument('--direct', action='store_true', help='Explicitly acknowledge direct public egress')
    args = parser.parse_args(argv)
    if args.execute and not args.direct:
        parser.error('--execute requires an explicitly approved --direct route')
    def direct(host, port, *, timeout):
        return socket.create_connection((host, port), timeout=timeout)
    try:
        print(json.dumps(probe(execute=args.execute, open_stream=direct if args.direct else None)))
    except Exception:
        # No raw errors/headers/tickets: a failure cannot trigger another request.
        print(json.dumps({'executed': args.execute, 'issued': False, 'error': 'probe_failed_no_retry'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
