# WinServerRAG — MCP 接続ガイド

Claude Cowork / Claude Desktop / 任意の MCP クライアントから、
**WinServerRAG が索引した Google Drive 共有フォルダを検索**できるようにします。

## 全体像

```
[別 PC / Claude Cowork]
  │
  │  MCP (Streamable HTTP + Basic Auth)
  ▼
[公開トンネル]  (Cloudflare Tunnel / Tailscale Funnel / ngrok)
  │
  ▼
[Windows の WinServerRAG Control API]
  │  /mcp  エンドポイント
  ▼
[PostgreSQL + pgvector]  ← 29 共有ドライブを索引済み
```

---

## 1. サーバー側 (Windows) の前提

- WinServerRAG がインストール済みかつ API 稼働中 (`http://127.0.0.1:17600/mcp` が応答)
- Windows モニターの **MCP 検索設定** タブで検索対象ドライブの `search_enabled` を ON にしておく
- MCP ログインユーザーが `public.mcp_users` に登録済み（`/api/mcp/users` で作成、または `WINSERVERRAG_SEED_USERS="user1:pw1,user2:pw2"` の環境変数で初回 seed）

## 2. リモート MCP エンドポイント (AWS サーバーレス)

MCP クライアント (Claude Cowork 等) から Windows 機に届くようにする場合、
本プロジェクトでは **AWS API Gateway + Lambda + SQS** のサーバーレスパイプ
を採用する。`src/aws_bridge.py` が SQS を long-poll して応答を返す。

- インフラ定義: [infra/aws/](./infra/aws/) (Terraform)
- 公開 URL: `terraform apply` 出力の `mcp_endpoint`
- 詳細フロー: [docs/EVOLUTION.md](./docs/EVOLUTION.md) Phase 3 参照

**採用しない方式** (過去の選択肢、今は使わない):
- ~~Cloudflare Quick Tunnel (`trycloudflare.com` ランダム URL)~~ — 管理
  API 全体が公開されてしまうため、v0.5 で AWS 経由に切替。
- ~~Tailscale funnel~~ — 便利だがチーム拡張時に個別招待運用が重い。

AWS を使わず LAN / 社内限定で公開したい場合は、Tailscale などの
別 VPN 経由を自分で構成する。その場合も **管理 API (port 17600) を直接
公開しない** こと (Basic Auth は MCP だけに掛かっている)。

### C. ngrok (お試し)

```powershell
winget install Ngrok.Ngrok
ngrok config add-authtoken <TOKEN>
ngrok http 17600
```

---

## 3. クライアント側の設定

### Claude Desktop / Claude Code (MCP HTTP client)

クライアントの `mcp.json` 或いは設定画面で新規サーバーを追加:

```json
{
  "mcpServers": {
    "winserverrag": {
      "type": "http",
      "url": "https://<あなたの公開URL>/mcp",
      "headers": {
        "Authorization": "Basic <BASE64(username:password)>"
      }
    }
  }
}
```

Base64 作成例 (PowerShell):

```powershell
[Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("***:***"))
```

### Claude Cowork

リモート MCP サーバー追加 UI で:

- **URL**: `https://<あなたの公開URL>/mcp`
- **認証方式**: HTTP Basic Auth
- **ユーザー名**: 管理者から払い出されたアカウント
- **パスワード**: 発行時に受け取った値（共有時は 20文字以上のランダム文字列を推奨）

## 4. 提供ツール

初期化後、クライアントから以下のツールが呼べます:

| tool | 引数 | 説明 |
|---|---|---|
| `list_drives` | — | MCP 検索スコープ (`search_enabled=TRUE`) のドライブ一覧 |
| `search` | `query: str, n_results: int=10, owner: str?` | セマンティック検索 (cos 類似) 全スコープ横断。チャンク単位で Top-N を返す |
| `lookup` | `url: str` | Drive の URL から全チャンクを取得。スプレッドシートは `?gid=...` 指定シート優先 |
| `stats` | — | スコープ内の総チャンク/総ファイル数、ドライブ別内訳 |

## 5. 動作確認 (cURL)

```bash
# init
curl -X POST https://<URL>/mcp/ \
  -u ***:*** \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

`event: message` + `data: {... "serverInfo":{"name":"WinServerRAG",...} ...}` が返れば接続 OK。

## 6. セキュリティ注意

- **弱いパスワードを設定しない** (20文字以上のランダム推奨)。変更は `/mcp/users/{username}/password` または UI の MCP タブから
- 公開トンネルを使う場合、**必ず Basic Auth を有効に** (当サーバーは強制)
- トンネル経由での公開は信頼できる人にのみ URL を渡す
- ユーザーの削除は Windows モニター → MCP 検索設定タブ → ユーザーテーブルの [削除]

## 7. エンドポイントまとめ

| URL | 用途 |
|---|---|
| `https://<URL>/mcp/` | MCP Streamable HTTP (JSON-RPC 2.0) |
| `https://<URL>/` | ブラウザ用管理画面 (Basic Auth 不要、localhost 閉じ) |
| `http://127.0.0.1:17600/api/...` | 管理用 REST API (同上) |

## 8. トラブルシューティング

**401 Unauthorized** — Basic Auth のユーザー名/パスワードを再確認。`***:***` を Base64 して送ったか。

**307 Temporary Redirect → Auth Header が落ちる** — 末尾スラッシュ `/mcp/` を直接指定。クライアントによっては Authorization を redirect 時に drop する。

**500 "Task group is not initialized"** — サーバー側の lifespan が立ち上がっていない。Control API を再起動して改善するか確認。

**ツール呼び出しは通るが結果 0 件** — サーバー側 Monitor で **MCP 検索設定** タブを開き、該当ドライブの検索 ON/OFF トグルを ON に。`list_drives` で可視化。
