// wizard.js — Setup Wizard renderer.
//
// State machine: linear steps 0..8. Each step is a function that
// renders into #step-host and returns { canAdvance: () => bool,
// onNext: async () => bool | "stay" }.
"use strict";

const $ = (id) => document.getElementById(id);
const W = window.wizard;

// In-memory accumulator of values the user enters across steps.
const STATE = {
  // Defaults; overridden by readConfig() in step 0 if a config exists.
  google: {
    GOOGLE_EMAIL: "",
    GOOGLE_OAUTH_CLIENT_ID: "",
    GOOGLE_OAUTH_CLIENT_SECRET: "",
    GOOGLE_TOKEN_PATH: "token.pickle",
  },
  postgres: {
    PGHOST: "localhost",
    PGPORT: "5432",
    PGUSER: "postgres",
    PGPASSWORD: "",
    PGDATABASE: "winserverrag",
  },
  indexing: {
    EMBEDDING_MODEL: "paraphrase-multilingual-mpnet-base-v2",
    EMBEDDING_DEVICE: "auto",
    BATCH_SIZE: "64",
    DAEMON_WORKER_THREADS: "4",
  },
  api: {
    API_HOST: "127.0.0.1",
    API_PORT: "17600",
    API_BEARER_TOKEN: "",
  },
  // Step-local
  credentialsPath: null,
  credentialsCopiedTo: null,
  pgTestResult: null,
  prereqResult: null,
  dbInitResult: null,
  serviceStartResult: null,
};

let stepIdx = 0;
// v1.3.3: was 9 — step 5 (Apply / 書き出し) folded into step 4 onNext.
const TOTAL_STEPS = 8;

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text || "";
  el.className = "status" + (kind ? " " + kind : "");
}
function setNext(label, enabled) {
  const b = $("next");
  b.textContent = label || "次へ";
  b.disabled = !enabled;
}
function setBack(enabled) { $("back").disabled = !enabled; }

function renderPip() {
  const pip = $("pip");
  pip.innerHTML = "";
  for (let i = 0; i < TOTAL_STEPS; i++) {
    const dot = document.createElement("div");
    dot.className = "pip" + (i === stepIdx ? " active" : i < stepIdx ? " done" : "");
    pip.appendChild(dot);
  }
}

// ---------------------------------------------------------------------
// Step renderers
// ---------------------------------------------------------------------
const steps = [
  // 0. Welcome + load existing config if any
  {
    title: "ようこそ",
    render: async () => {
      setBack(false);
      setStatus("既存の設定を読み込み中...", null);
      const existing = await W.readConfig();
      // v1.3.2 fix B4: pre-fill STATE from existing config values so
      // wizard re-run doesn't erase the operator's prior input.
      // Each group merges only the keys it cares about; unknown keys
      // are ignored. Empty/blank values fall through to defaults.
      if (existing && existing.values) {
        const v = existing.values;
        const m = (group, keys) => {
          for (const k of keys) {
            if (v[k] !== undefined && v[k] !== "") group[k] = v[k];
          }
        };
        m(STATE.google,   ["GOOGLE_EMAIL", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_TOKEN_PATH"]);
        m(STATE.postgres, ["PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"]);
        m(STATE.indexing, ["EMBEDDING_MODEL", "EMBEDDING_DEVICE", "BATCH_SIZE", "DAEMON_WORKER_THREADS"]);
        m(STATE.api,      ["API_HOST", "API_PORT", "API_BEARER_TOKEN"]);
      }
      setStatus("");
      $("step-host").innerHTML = `
        <div class="hero">
          <div class="emoji">🏁</div>
          <h2>WinServerRAG セットアップへようこそ</h2>
          <p>このウィザードは、サービスを動かすために必要な設定<br/>
             (PostgreSQL 接続 / Google OAuth / インデックス設定 など) を順番に行います。<br/>
             所要時間: 5〜10分</p>
        </div>
      `;
      setNext("はじめる", true);
    },
    onNext: async () => true,
  },

  // 1. Prerequisites — PG service running? services registered?
  {
    title: "前提チェック",
    render: async () => {
      setBack(true);
      setStatus("検出中...", null);
      const r = await W.prereqCheck();
      STATE.prereqResult = r;
      const pg = r.postgres;
      const pgOk = pg.exists && pg.running;
      const pgIcon = pgOk ? "✅" : pg.exists ? "⚠️" : "❌";
      const pgClass = pgOk ? "ok" : pg.exists ? "warn" : "err";
      const pgText = pg.exists
        ? `postgresql-x64-${pg.version} サービス検出 (${pg.running ? "実行中" : "停止中"})`
        : "PostgreSQL サービス未検出 (postgresql-x64-15/16/17 のいずれも見つからず)";
      const apiOk = r.winserverrag.apiRegistered;
      const dmOk = r.winserverrag.daemonRegistered;
      const svcText = (apiOk && dmOk)
        ? "WinServerRAG-API / WinServerRAG-Daemon 登録済"
        : "WinServerRAG サービスが未登録 (インストーラーが正常完了していません)";
      const svcClass = (apiOk && dmOk) ? "ok" : "err";
      $("step-host").innerHTML = `
        <h2>前提チェック</h2>
        <p class="subtitle">先に進むのに必要な環境が整っているか確認します。</p>
        <div class="card ${pgClass}">
          <div class="icon">${pgIcon}</div>
          <div class="body">
            <div class="name">PostgreSQL</div>
            <div class="detail">${pgText}</div>
          </div>
        </div>
        <div class="card ${svcClass}">
          <div class="icon">${(apiOk && dmOk) ? "✅" : "❌"}</div>
          <div class="body">
            <div class="name">WinServerRAG サービス</div>
            <div class="detail">${svcText}</div>
          </div>
        </div>
        ${pgOk && apiOk && dmOk ? '' :
          '<div class="msg warn">⚠ 上のいずれかが ❌ の場合は、先にインストーラーを完走 / PostgreSQL を起動してから戻ってきてください。「次へ」で進むことはできますが、ステップ 7 以降で失敗します。</div>'}
      `;
      setStatus("");
      setNext("次へ", true);
    },
    onNext: async () => true,
  },

  // 2. PostgreSQL connection
  {
    title: "PostgreSQL 接続",
    render: async () => {
      setBack(true);
      const v = STATE.postgres;
      $("step-host").innerHTML = `
        <h2>PostgreSQL 接続</h2>
        <p class="subtitle">サービスが PG に接続するための情報を入力します。</p>
        <div class="row">
          <div class="field">
            <label>ホスト</label>
            <input type="text" id="pg-host" value="${v.PGHOST}" />
          </div>
          <div class="field" style="max-width:120px">
            <label>ポート</label>
            <input type="text" id="pg-port" value="${v.PGPORT}" />
          </div>
        </div>
        <div class="row">
          <div class="field">
            <label>ユーザー</label>
            <input type="text" id="pg-user" value="${v.PGUSER}" />
          </div>
          <div class="field">
            <label>データベース</label>
            <input type="text" id="pg-db" value="${v.PGDATABASE}" />
            <div class="hint">db_init 実行時に作成されます (まだ無くて OK)</div>
          </div>
        </div>
        <div class="field">
          <label>パスワード</label>
          <input type="password" id="pg-pw" value="${v.PGPASSWORD}" />
        </div>
        <button id="pg-test" class="btn test">接続テスト (TCP)</button>
        <div id="pg-test-result"></div>
      `;
      $("pg-test").addEventListener("click", async () => {
        // v1.3.4: clear previous result the moment the test fires so the
        // user doesn't see stale "❌ 接続失敗: ..." lingering during the
        // new test's network roundtrip. Status footer shows progress.
        $("pg-test-result").innerHTML = `<div class="msg" style="opacity:.7">⏳ 接続テスト中...</div>`;
        setStatus("接続テスト中...", null);
        const params = collectPg();
        const r = await W.testPgConnection(params);
        STATE.pgTestResult = r;
        const box = $("pg-test-result");
        if (r.ok) {
          const note = r.note ? `<div class="hint" style="margin-top:4px">${escapeHtml(r.note)}</div>` : "";
          box.innerHTML = `<div class="msg ok">✅ ${escapeHtml(r.stdout || "接続成功")}</div>${note}`;
          setStatus("接続 OK", "ok");
        } else {
          // Use <pre> so multi-line diag (with code=N etc) wraps nicely.
          box.innerHTML = `<div class="msg err">❌ 接続失敗:<pre style="margin:4px 0 0;white-space:pre-wrap;font-size:11px">${escapeHtml(r.error)}</pre></div>`;
          setStatus("接続失敗 (テストは informational、「次へ」で進めます)", "err");
        }
      });
      setNext("次へ", true);
    },
    onNext: async () => {
      Object.assign(STATE.postgres, collectPg());
      return true;
    },
  },

  // 3. Google OAuth — credentials.json picker
  {
    title: "Google OAuth",
    render: async () => {
      setBack(true);
      const v = STATE.google;
      $("step-host").innerHTML = `
        <h2>Google OAuth</h2>
        <p class="subtitle">Drive にアクセスするための OAuth credentials.json を指定します。</p>
        <div class="field">
          <label>Google アカウント (サービスが使うメール)</label>
          <input type="text" id="g-email" value="${v.GOOGLE_EMAIL}" placeholder="you@example.com" />
        </div>
        <div class="field">
          <label>OAuth Client ID</label>
          <input type="text" id="g-cid" value="${v.GOOGLE_OAUTH_CLIENT_ID}" placeholder="...apps.googleusercontent.com" />
        </div>
        <div class="field">
          <label>OAuth Client Secret</label>
          <input type="password" id="g-csec" value="${v.GOOGLE_OAUTH_CLIENT_SECRET}" />
        </div>
        <div class="msg warn">
          推奨: <strong>credentials.json を先に選択</strong>すると Client ID / Secret は<strong>自動入力</strong>されます。<br/>
          credentials.json は Google Cloud Console からダウンロードした OAuth client ファイル (installed app)。<br/>
          コピー先: <code>%ProgramData%\\WinServerRAG\\config\\credentials.json</code><br/>
          OAuth の本物の動作テスト (token.pickle 作成) はこのウィザードでは行いません。Step 8 で API サービスが初回起動した時に、ブラウザが開いて Google 認証画面に進みます。<br/>
          このステップでは: ① credentials.json を選択 ② Google アカウント (Drive オーナーのメール) を入力 ③ 次へ。
        </div>
        <button id="pick-cred" class="btn test">credentials.json を選択...</button>
        <div id="cred-result">${STATE.credentialsCopiedTo ? `<div class="msg ok">✅ コピー済: ${escapeHtml(STATE.credentialsCopiedTo)}</div>` : ""}</div>
      `;
      $("pick-cred").addEventListener("click", async () => {
        const pick = await W.pickCredentials();
        if (pick.canceled) return;
        STATE.credentialsPath = pick.path;
        const r = await W.copyCredentials(pick.path);
        if (r.ok) {
          STATE.credentialsCopiedTo = r.dest;
          // v1.3.3: auto-fill client_id / secret from the picked file so
          // the operator doesn't have to copy-paste the same values into
          // the form. STATE update happens via the input fields' .value =
          // assignment so the next snapshotCurrentStep / collectGoogle
          // call captures them.
          let autofilled = [];
          if (r.clientId) {
            $("g-cid").value = r.clientId;
            STATE.google.GOOGLE_OAUTH_CLIENT_ID = r.clientId;
            autofilled.push("Client ID");
          }
          if (r.clientSecret) {
            $("g-csec").value = r.clientSecret;
            STATE.google.GOOGLE_OAUTH_CLIENT_SECRET = r.clientSecret;
            autofilled.push("Client Secret");
          }
          const autofillNote = autofilled.length
            ? `<div class="msg ok">🔑 自動入力: ${autofilled.join(" / ")} (credentials.json から)</div>`
            : "";
          $("cred-result").innerHTML =
            `<div class="msg ok">✅ コピー済: ${escapeHtml(r.dest)}</div>` + autofillNote;
          setStatus("credentials.json コピー完了 + 自動入力", "ok");
        } else {
          $("cred-result").innerHTML = `<div class="msg err">❌ コピー失敗: ${escapeHtml(r.error)}</div>`;
          setStatus("コピー失敗", "err");
        }
      });
      setNext("次へ", true);
    },
    onNext: async () => {
      STATE.google.GOOGLE_EMAIL = $("g-email").value.trim();
      STATE.google.GOOGLE_OAUTH_CLIENT_ID = $("g-cid").value.trim();
      STATE.google.GOOGLE_OAUTH_CLIENT_SECRET = $("g-csec").value.trim();
      const missing = ["GOOGLE_EMAIL", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"]
        .filter((k) => !STATE.google[k]);
      if (missing.length) {
        setStatus(`入力不足: ${missing.join(", ")}`, "err");
        return "stay";
      }
      return true;
    },
  },

  // 4. Indexing settings
  {
    title: "インデックス設定",
    render: async () => {
      setBack(true);
      const v = STATE.indexing;
      // v1.3.3: dropdown of curated 768-dim multilingual models. The
      // PG schema uses `vector(768)` (config.py EMBEDDING_DIM=768), so
      // models with different dim would crash on insert. Free-form
      // text input let users type model names that broke the daemon
      // silently. If config.v2.env had a custom value not in this
      // list (e.g., from manual edit), it shows up as a "(config から)"
      // option to preserve existing setups.
      const KNOWN_MODELS = [
        { value: "paraphrase-multilingual-mpnet-base-v2",
          label: "paraphrase-multilingual-mpnet-base-v2 (推奨、768d、~500MB、日本語+50言語)" },
        { value: "intfloat/multilingual-e5-base",
          label: "intfloat/multilingual-e5-base (新世代 E5、768d、~500MB)" },
      ];
      const cur = v.EMBEDDING_MODEL || KNOWN_MODELS[0].value;
      const isKnown = KNOWN_MODELS.some((m) => m.value === cur);
      const opts = KNOWN_MODELS.map((m) => {
        const sel = m.value === cur ? " selected" : "";
        return `<option value="${escapeHtml(m.value)}"${sel}>${escapeHtml(m.label)}</option>`;
      }).join("");
      const customOpt = !isKnown
        ? `<option value="${escapeHtml(cur)}" selected>${escapeHtml(cur)} (既存 config から)</option>`
        : "";
      $("step-host").innerHTML = `
        <h2>インデックス設定</h2>
        <p class="subtitle">推奨のままで問題ありません。後から config.v2.env を編集して変更できます (要 reindex)。</p>
        <div class="field">
          <label>埋め込みモデル</label>
          <select id="ix-model">${opts}${customOpt}</select>
          <div class="hint">PG schema は vector(768) 固定なので 768 次元モデルのみ。変更すると初回起動時にモデルを再ダウンロード + 全 chunk を reindex (~時間〜日)。</div>
        </div>
        <div class="row">
          <div class="field">
            <label>デバイス</label>
            <select id="ix-device">
              <option value="auto"${v.EMBEDDING_DEVICE==="auto"?" selected":""}>auto (CUDA 検出時に GPU)</option>
              <option value="cuda"${v.EMBEDDING_DEVICE==="cuda"?" selected":""}>cuda (強制 GPU)</option>
              <option value="cpu"${v.EMBEDDING_DEVICE==="cpu"?" selected":""}>cpu (強制 CPU)</option>
            </select>
          </div>
          <div class="field">
            <label>バッチサイズ</label>
            <input type="number" id="ix-batch" min="1" max="256" value="${v.BATCH_SIZE}" />
          </div>
          <div class="field">
            <label>ワーカースレッド数</label>
            <input type="number" id="ix-threads" min="1" max="10" value="${v.DAEMON_WORKER_THREADS}" />
            <div class="hint">CPU bound: 多すぎると埋め込みが詰まる</div>
          </div>
        </div>
      `;
      setNext("次へ", true);
    },
    onNext: async () => {
      STATE.indexing.EMBEDDING_MODEL = $("ix-model").value.trim();
      STATE.indexing.EMBEDDING_DEVICE = $("ix-device").value;
      STATE.indexing.BATCH_SIZE = $("ix-batch").value || "64";
      STATE.indexing.DAEMON_WORKER_THREADS = $("ix-threads").value || "4";
      // v1.3.3: auto-write config.v2.env on transition out of step 4.
      // The previous "Step 5: Apply" page asking the operator to confirm
      // and click "書き出す" was redundant — they had no choice of
      // destination, no preview, and the only info added was a 2-line
      // warning. The action is now implicit; failure surfaces in the
      // wizard footer status. On EPERM, fall through to a clearer
      // error message that points at the icacls grant requirement.
      setStatus("config.v2.env に書き出し中...", null);
      const values = {
        ...STATE.google,
        ...STATE.postgres,
        ...STATE.indexing,
        ...STATE.api,
      };
      const r = await W.writeConfig(values);
      if (r.ok) {
        setStatus(`config.v2.env 保存完了 (${r.bytes} bytes)`, "ok");
        return true;
      }
      // EPERM = config dir ACL doesn't include the operator user.
      // Inline the icacls fix as a copy-paste hint so the operator
      // can resolve in one elevated command.
      const isAcl = /EPERM|EACCES|operation not permitted|access is denied/i.test(r.error || "");
      const hint = isAcl
        ? `\n\n権限不足です。管理者 PowerShell で以下を実行してから再試行してください:\n  icacls "C:\\ProgramData\\WinServerRAG\\config" /grant "$env:USERNAME:(OI)(CI)F"`
        : "";
      setStatus(`書き出し失敗: ${r.error}${hint}`, "err");
      // Inline status only — keep the user on this step but don't
      // force a separate "Apply" page.
      return "stay";
    },
  },

  // 5 (was 6): db_init
  {
    title: "DB スキーマ初期化",
    render: async () => {
      setBack(true);
      $("step-host").innerHTML = `
        <h2>DB スキーマ初期化</h2>
        <p class="subtitle">winserverrag-dbinit.exe を実行して PostgreSQL に schema を適用します。</p>
        <div class="log-box" id="log-box"><div class="line">(まだ実行していません)</div></div>
      `;
      setNext("実行する", true);
    },
    onNext: async () => {
      const box = $("log-box");
      box.innerHTML = "";
      const append = (line, kind) => {
        const div = document.createElement("div");
        div.className = "line" + (kind ? " " + kind : "");
        div.textContent = line;
        box.appendChild(div);
        box.scrollTop = box.scrollHeight;
      };
      // v1.3.2 fix B3: capture the off() disposer so re-runs don't
      // accumulate listeners. We detach BEFORE returning regardless
      // of success/fail. Subsequent runs start with a fresh listener.
      const offLog = W.onDbInitLog((line) => append(line));
      setStatus("db_init 実行中...", null);
      setNext("実行中...", false);
      try {
        const r = await W.runDbInit();
        STATE.dbInitResult = r;
        if (r.ok) {
          append("--- 完了 (exit 0) ---", "ok");
          setStatus("db_init 完了", "ok");
          setNext("次へ", true);
          return true;
        } else {
          append(`--- 失敗 (exit ${r.exitCode}): ${r.error || ""} ---`, "err");
          setStatus("db_init 失敗", "err");
          setNext("再実行", true);
          return "stay";
        }
      } finally {
        try { if (typeof offLog === "function") offLog(); } catch {}
      }
    },
  },

  // 6 (was 7): Start services
  {
    title: "サービス起動",
    render: async () => {
      setBack(true);
      $("step-host").innerHTML = `
        <h2>サービス起動</h2>
        <p class="subtitle">WinServerRAG-API → WinServerRAG-Daemon の順で起動します。</p>
        <div class="card" id="svc-api">
          <div class="icon">⚙️</div>
          <div class="body">
            <div class="name">WinServerRAG-API</div>
            <div class="detail">未起動</div>
          </div>
        </div>
        <div class="card" id="svc-daemon">
          <div class="icon">⚙️</div>
          <div class="body">
            <div class="name">WinServerRAG-Daemon</div>
            <div class="detail">未起動</div>
          </div>
        </div>
        <div id="svc-result"></div>
      `;
      setNext("起動する", true);
    },
    onNext: async () => {
      setStatus("起動中...", null);
      setNext("起動中...", false);
      const r = await W.startServices();
      STATE.serviceStartResult = r;
      const status = await W.serviceStatus();
      const apiCard = $("svc-api");
      const dmCard = $("svc-daemon");
      apiCard.classList.toggle("ok", status.api.kind === "running");
      apiCard.classList.toggle("err", status.api.kind === "stopped" || status.api.kind === "unknown");
      apiCard.querySelector(".detail").textContent = `state: ${status.api.kind}`;
      apiCard.querySelector(".icon").textContent = status.api.kind === "running" ? "✅" : "❌";
      dmCard.classList.toggle("ok", status.daemon.kind === "running");
      dmCard.classList.toggle("err", status.daemon.kind === "stopped" || status.daemon.kind === "unknown");
      dmCard.querySelector(".detail").textContent = `state: ${status.daemon.kind}`;
      dmCard.querySelector(".icon").textContent = status.daemon.kind === "running" ? "✅" : "❌";
      if (r.ok) {
        setStatus("両サービス起動済", "ok");
        setNext("次へ", true);
        return true;
      } else {
        const stage = r.stage || "?";
        const msg = (r.result && r.result.message) || r.error || "(unknown)";
        $("svc-result").innerHTML = `<div class="msg err">❌ ${escapeHtml(stage)} 起動失敗: ${escapeHtml(msg)}</div>`;
        setStatus("起動失敗", "err");
        setNext("再試行", true);
        return "stay";
      }
    },
  },

  // 7 (was 8): Done
  {
    title: "完了",
    render: async () => {
      setBack(false);
      $("step-host").innerHTML = `
        <div class="hero">
          <div class="emoji">🎉</div>
          <h2>セットアップ完了</h2>
          <p>WinServerRAG が起動しました。<br/>
             下のボタンから web console を開くか、Mini Monitor を起動できます。</p>
        </div>
        <div class="action-grid">
          <button class="btn primary" id="open-web">Web Console を開く<br/><small style="opacity:.7">http://127.0.0.1:17600/</small></button>
          <button class="btn" id="launch-mini">Mini Monitor を起動</button>
        </div>
      `;
      $("open-web").addEventListener("click", () => W.openWebConsole());
      $("launch-mini").addEventListener("click", () => { W.launchMini(); W.closeWizard(); });
      setStatus("");
      setNext("ウィザードを閉じる", true);
    },
    onNext: async () => {
      W.closeWizard();
      return false; // no advance
    },
  },
];

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------
function collectPg() {
  return {
    PGHOST: $("pg-host").value.trim() || "localhost",
    PGPORT: $("pg-port").value.trim() || "5432",
    PGUSER: $("pg-user").value.trim() || "postgres",
    PGPASSWORD: $("pg-pw").value,
    PGDATABASE: $("pg-db").value.trim() || "winserverrag",
  };
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function go() {
  renderPip();
  await steps[stepIdx].render();
}

// v1.3.2 fix B5: snapshot the current step's form values before any
// transition (Back or Next). Without this, hitting Back after typing
// a PG password (step 2) loses the input. Only steps with collect*
// helpers participate; others are no-op.
function snapshotCurrentStep() {
  try {
    if (stepIdx === 2 && document.getElementById("pg-host")) {
      Object.assign(STATE.postgres, collectPg());
    }
    if (stepIdx === 3 && document.getElementById("g-email")) {
      STATE.google.GOOGLE_EMAIL = $("g-email").value.trim();
      STATE.google.GOOGLE_OAUTH_CLIENT_ID = $("g-cid").value.trim();
      STATE.google.GOOGLE_OAUTH_CLIENT_SECRET = $("g-csec").value;
    }
    if (stepIdx === 4 && document.getElementById("ix-model")) {
      STATE.indexing.EMBEDDING_MODEL = $("ix-model").value.trim();
      STATE.indexing.EMBEDDING_DEVICE = $("ix-device").value;
      STATE.indexing.BATCH_SIZE = $("ix-batch").value || "64";
      STATE.indexing.DAEMON_WORKER_THREADS = $("ix-threads").value || "4";
    }
  } catch {
    // DOM not ready / element missing — best-effort snapshot only.
  }
}

// ---------------------------------------------------------------------
// Wire-up
// ---------------------------------------------------------------------
// v1.3.2 fix B2: inflight guard. Without this, Back fires while
// onNext (e.g. db_init child process running in step 6) is in flight,
// state machine corrupts: stepIdx changes mid-await, the resolving
// onNext writes to elements from a stale step.
let _busyOnNext = false;

window.addEventListener("DOMContentLoaded", () => {
  $("back").addEventListener("click", () => {
    if (_busyOnNext || stepIdx === 0) return;
    snapshotCurrentStep();
    stepIdx--;
    go();
  });
  $("next").addEventListener("click", async () => {
    if (_busyOnNext) return;
    _busyOnNext = true;
    try {
      const result = await steps[stepIdx].onNext();
      if (result === true && stepIdx < TOTAL_STEPS - 1) {
        stepIdx++;
        go();
      }
      // result === "stay" → render didn't change, status already set
    } finally {
      _busyOnNext = false;
    }
  });
  go();
});
