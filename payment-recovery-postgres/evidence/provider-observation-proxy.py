#!/usr/bin/env python3
import argparse
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--listen",default="127.0.0.1:18085")
    ap.add_argument("--upstream",required=True)
    ap.add_argument("--mode",choices=["pass","not-found","stale","identity-conflict"],required=True)
    args=ap.parse_args()

    parsed=urlsplit(args.upstream.rstrip("/"))
    if parsed.scheme!="http" or not parsed.hostname:
        raise SystemExit("--upstream must be http://host[:port]")
    upstream_host=parsed.hostname
    upstream_port=parsed.port or 80
    listen_host,listen_port=args.listen.rsplit(":",1)

    class Handler(BaseHTTPRequestHandler):
        protocol_version="HTTP/1.1"
        def log_message(self,fmt,*values):
            print(json.dumps({"component":"provider-observation-proxy","message":fmt%values}),flush=True)
        def do_GET(self):
            if self.path=="/health":
                self._json(200,{"status":"ok","mode":args.mode})
                return
            if args.mode=="not-found":
                self._json(404,{"error":{"code":"payment_not_found","message":"synthetic observation gap"}})
                return
            conn=http.client.HTTPConnection(upstream_host,upstream_port,timeout=10)
            try:
                conn.request("GET",self.path,headers={"Accept":"application/json"})
                response=conn.getresponse()
                body=response.read()
                status=response.status
            finally:
                conn.close()
            if status==200 and args.mode in {"stale","identity-conflict"}:
                payload=json.loads(body.decode("utf-8"))
                if args.mode=="stale":
                    payload["status"]="pending"
                    payload["authorized_amount"]=0
                    payload["captured_amount"]=0
                    payload["refunded_amount"]=0
                    body=json.dumps(payload,separators=(",",":")).encode()
                    self._bytes(200,body,{"X-Payment-Recovery-Visibility":"STALE"})
                    return
                payload["id"]="conflicting-"+str(payload.get("id","unknown"))
                body=json.dumps(payload,separators=(",",":")).encode()
                self._bytes(200,body)
                return
            self._bytes(status,body)
        def _json(self,status,payload):
            self._bytes(status,json.dumps(payload,separators=(",",":")).encode())
        def _bytes(self,status,body,headers=None):
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            for name,value in (headers or {}).items():
                self.send_header(name,value)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Connection","close")
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((listen_host,int(listen_port)),Handler).serve_forever()

if __name__=="__main__":
    main()
