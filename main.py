from flask import Flask, jsonify
import requests
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # CORS 허용

@app.route('/')
def home():
    return '✅ OKX Proxy Server Running'

@app.route('/swap')
def get_swap_instruments():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json'
        }
        url = 'https://www.okx.com/api/v5/public/instruments?instType=SWAP'
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        return jsonify(res.json()['data'])  # data 필드만 반환
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
