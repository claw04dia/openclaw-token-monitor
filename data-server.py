#!/usr/bin/env python3
"""Server dati real-time per Telegram Token Monitor MiniApp.
Serve /api/data con sessioni, prezzi e stats reali dal sistema."""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HISTORY_PATH = "/tmp/claw04-telegram-miniapp/history.json"
TOKEN_USAGE_PATH = "/home/claw04/.openclaw/workspace/memory/token-usage.json"

MODEL_MAP = {
    'deepseek/deepseek-v4-flash': 'DeepSeek V4 Flash',
    'deepseek/deepseek-v4-pro': 'DeepSeek V4 Pro',
    'moonshotai/kimi-k2.6': 'Kimi K2.6',
    'qwen/qwen3-coder-plus': 'Qwen3 Coder+',
    'google/gemma-4-31b-it': 'Gemma 4 31B',
    'xiaomi/mimo-v2-pro': 'Mimo V2 Pro'
}

class DataHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/data':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            data = self._build_data()
            self.wfile.write(json.dumps(data, indent=2).encode())
        elif parsed.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>Token Monitor API</h1><p>GET <a href="/api/data">/api/data</a></p>')
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'404')
    
    def _build_data(self):
        result = {"updatedAt": None, "prices": [], "sessions": []}
        
        # Leggi history.json
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH) as f:
                hist = json.load(f)
            result["updatedAt"] = hist.get("updatedAt")
            result["dailyStats"] = hist.get("dailyStats", [])
            
            # Prezzi
            for mid, p in hist.get("modelPricing", {}).items():
                name = MODEL_MAP.get(mid, mid)
                result["prices"].append({
                    "model": name, "id": mid,
                    "inputPrice": p["in"], "outputPrice": p["out"]
                })
            
            # Sessioni
            for s in hist.get("sessions", []):
                if s.get("tokensIn", 0) > 200:
                    result["sessions"].append({
                        "date": s["date"],
                        "topic": s["topic"],
                        "model": "DeepSeek V4 Flash",
                        "tokensIn": s["tokensIn"],
                        "tokensOut": s["tokensOut"],
                        "cost": s.get("costUsd", 0)
                    })
        
        # Leggi token-usage.json per prezzi aggiornati
        if os.path.exists(TOKEN_USAGE_PATH):
            with open(TOKEN_USAGE_PATH) as f:
                usage = json.load(f)
            result["dailyBudgetUsd"] = usage.get("dailyBudgetUsd", 1.0)
            result["totalCostUsd"] = usage.get("totalCostUsd", 0)
        
        return result

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8899))
    server = HTTPServer(('0.0.0.0', port), DataHandler)
    print(f"📊 Data server on http://0.0.0.0:{port}/api/data")
    server.serve_forever()
