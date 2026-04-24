# docs/images/

README・ドキュメントで参照する画像・GIF の配置先。

## 期待するファイル（未作成。メンテナ向けメモ）

| ファイル名 | 用途 | 撮影方法 |
|---|---|---|
| `monitor.gif` | README デモ節のモニター実行動画 | `./run_build.sh` 起動中、ターミナルの録画（例: macOS Screenshot → ビデオ、Peek, LICEcap, ttyrec → ttygif） |
| `mcp_search.png` | README デモ節の Claude Code 検索結果 | Claude Code で `search` ツール実行時のスクリーンショット |
| `folder_tree.png` | MCP の `folder_tree` ツール出力例 | 同上 |
| `architecture.svg` | architecture.md の Mermaid を SVG 書き出ししたもの（任意） | Mermaid Live Editor の Download SVG |

## 命名規則

- 小文字 + アンダースコア（`my_image.png`）
- 用途が分かる名前（`monitor_phase3.gif` など）
- スクリーンショットは可能なら PNG、動画は GIF（ファイルサイズ < 5MB 推奨）

## 撮影時の注意

- **機密情報**: 実ファイル名・オーナー名・ドライブ ID が映らないようダミーデータで撮影する
- **解像度**: README の表示幅を想定し、長辺 1200px 程度まで
- **ファイルサイズ**: 巨大 GIF は LFS 利用を検討
