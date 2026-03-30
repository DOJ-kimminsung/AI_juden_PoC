#!/bin/bash
# ============================================================
# AI受電システム PoC — AWS EC2 (Ubuntu 22.04/24.04) セットアップ
# 実行方法: bash setup.sh
# ============================================================
set -e

echo "=== AI受電システム PoC セットアップ (AWS EC2) ==="

# --- Python確認 ---
python3 --version

# --- 仮想環境 ---
if [ ! -d ".venv" ]; then
    echo "→ 仮想環境を作成中..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "→ パッケージをインストール中..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# --- .envチェック ---
if [ ! -f ".env" ]; then
    echo ""
    echo "[警告] .env ファイルが見つかりません。"
    echo "  cp .env.example .env  を実行してAPIキーを設定してください。"
    exit 1
fi

echo ""
echo "=== セットアップ完了 ==="
echo ""
echo "【次の手順】"
echo ""
echo "1. サーバー起動テスト:"
echo "   source .venv/bin/activate"
echo "   uvicorn main:app --host 0.0.0.0 --port 8000"
echo ""
echo "2. ヘルスチェック確認:"
echo "   curl http://localhost:8000/health"
echo ""
echo "3. systemdサービスとして登録（常時起動）:"
echo "   sudo cp deploy/ai_phone.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable ai_phone"
echo "   sudo systemctl start ai_phone"
echo ""
echo "4. Nginx設定:"
echo "   sudo cp deploy/nginx.conf /etc/nginx/sites-available/ai_phone"
echo "   sudo ln -s /etc/nginx/sites-available/ai_phone /etc/nginx/sites-enabled/"
echo "   sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "5. SSL証明書取得 (sslip.ioドメイン使用):"
echo "   ELASTIC_IP=\$(curl -s ifconfig.me)"
echo "   DOMAIN=\"\${ELASTIC_IP//./-}.sslip.io\""
echo "   sudo certbot --nginx -d \$DOMAIN --non-interactive --agree-tos -m your@email.com"
echo ""
echo "6. Twilio Webhook URLを更新:"
echo "   https://\${ELASTIC_IP//./-}.sslip.io/incoming-call"
