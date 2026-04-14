# Windows Task Scheduler integration

Windows 版 sync_rotate 定期実行。Mac の `launchd/` と同等の役割。

## ファイル

| ファイル | 役割 |
|---|---|
| `run_sync_rotate.bat` | WSL 側 `run_sync_rotate.sh` を呼び出す shim |
| `register_sync_rotate_task.bat` | タスク登録（1分間隔、hidden、single-instance） |
| `unregister_sync_rotate_task.bat` | タスク削除 |

## 使い方

```cmd
cd scheduler\windows
register_sync_rotate_task.bat
```

管理者権限は不要（ログインユーザーのタスクとして登録）。
ログイン中のみ発火する設定。Windows をロックしてもタスクは動く。

## 設定のカスタマイズ

`run_sync_rotate.bat` の先頭3変数：

```bat
set WSL_DISTRO=Ubuntu
set WSL_USER=tobi
set DB_NUM=4
```

## 動作仕様

- **間隔**: 1 分（GUI 設定の最小値）
- **Hidden**: コンソール非表示（`-Hidden` 設定）
- **MultipleInstances=IgnoreNew**: 前回が動作中なら新規起動をスキップ
- **ExecutionTimeLimit**: 10 分（閾値を超えたら強制終了）
- **RunLevel**: Limited（昇格なし）
- **AllowStartIfOnBatteries**: バッテリ駆動でも動作

## 多重起動ガード（二段階）

1. **Windows 側**: `MultipleInstances=IgnoreNew` → Task Scheduler が新規起動をブロック
2. **WSL 側**: `sync_rotate.py` の `/tmp/gridworldrag_rotate.lock`（PID liveness probe, 20 分 stale fallback）

どちらかが失敗してももう一方で守られる。

## ログ

WSL 側の `/tmp/gridworldrag_sync.log` と `/tmp/gridworldrag_sync.err`。
Windows からは `\\wsl.localhost\Ubuntu\tmp\gridworldrag_sync.log` で閲覧可能。

`sync_rotate.py` 自身は `~/Library/Logs/gridworldrag/sync_rotate.log`
にも `RotatingFileHandler`（5MB × 3世代）で書き出す（macOS 慣例に従うパスだが Linux でも動作する）。

## g-stack 運用注意

1. **1分間隔はテスト用**。本番運用では 5 分推奨（Mac 側の launchd と合わせる）。
   `register_sync_rotate_task.bat` の `-RepetitionInterval (New-TimeSpan -Minutes 1)` を
   `-Minutes 5` に変更。Drive API クォータ消費が 5 倍違う。

2. **WSL コールドスタート遅延**。Windows 再起動直後の初回は Ubuntu distro の
   起動に 5〜10 秒かかる。sync_rotate 自体（変更0で約 7 秒）と合わせて
   初回のみ 15 秒程度。以降はホット。

3. **Mac との同時運用に注意**。同じ OAuth クライアント + 共有ドライブを
   Mac 側 launchd (5min) と WSL 側 (1min) の両方から叩くと、Drive Changes API
   のトークンが両者で交互に進み、どちらかが変更を取り逃す可能性あり。
   運用は **どちらか一方のみ**。

4. **PC スリープ時**。`-StartWhenAvailable` を付けているため、スリープ復帰直後に
   取り逃した1回分のみ実行（Mac launchd と同等挙動）。蓄積再生はしない。

5. **`MultipleInstances=IgnoreNew`** は Task Scheduler 2.0 API 必須。
   Windows 10/11 なら問題なし。
