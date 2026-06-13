-- Telegram Assistant Bot · SQLite 建表(含 FTS5)
-- 由 app/db/engine.py 在启动时执行(IF NOT EXISTS 幂等)

-- 用户与角色
CREATE TABLE IF NOT EXISTS users (
  tg_id        INTEGER PRIMARY KEY,
  username     TEXT, first_name TEXT,
  role         TEXT NOT NULL DEFAULT 'user',     -- superadmin|admin|user
  authorized   INTEGER NOT NULL DEFAULT 0,       -- 1=已授权(全功能全场景) 0=拒绝
  authorized_by INTEGER, authorized_at INTEGER,
  settings     TEXT DEFAULT '{}',
  created_at   INTEGER, updated_at INTEGER
);

-- 配额(每用户;calls 或 tokens 两种计量)
CREATE TABLE IF NOT EXISTS quotas (
  user_id    INTEGER NOT NULL,
  mode       TEXT NOT NULL,                       -- 'calls' | 'tokens'
  period     TEXT NOT NULL DEFAULT 'day',         -- 'day' | 'month' | 'total'
  limit_val  INTEGER NOT NULL,                    -- 上限;-1 = 无限
  used       INTEGER NOT NULL DEFAULT 0,
  window_start INTEGER, updated_at INTEGER,
  PRIMARY KEY (user_id, mode)
);

-- 用量流水(审计 / 统计)
CREATE TABLE IF NOT EXISTS usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, chat_id INTEGER,
  kind TEXT,                                      -- chat|image|video|tts|music|search|fetch
  calls INTEGER DEFAULT 1, tokens INTEGER DEFAULT 0,
  created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, created_at);

-- 会话
CREATE TABLE IF NOT EXISTS chats (
  chat_id INTEGER PRIMARY KEY, type TEXT, title TEXT,
  settings TEXT DEFAULT '{}', token_budget INTEGER DEFAULT 128000, created_at INTEGER
);

-- 对话原始消息(压缩前)
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER, user_id INTEGER,
  role TEXT, content TEXT, content_type TEXT DEFAULT 'text',
  tokens INTEGER DEFAULT 0, compacted INTEGER DEFAULT 0, created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, id);

-- 滚动摘要
CREATE TABLE IF NOT EXISTS summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER, summary TEXT, covers_up_to_id INTEGER, tokens INTEGER, created_at INTEGER
);

-- 持久记忆
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT, owner_id INTEGER, text TEXT, source TEXT,
  weight REAL DEFAULT 1.0, created_at INTEGER, last_used_at INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id');

-- memories ↔ memories_fts 同步触发器
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;

-- 生成任务(图/视/音/乐)—— 并发后台 worker 的状态源 + 重启恢复依据
CREATE TABLE IF NOT EXISTS generations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, chat_id INTEGER, kind TEXT, model TEXT, prompt TEXT,
  status TEXT,                                    -- queued|processing|success|failed
  task_id TEXT, file_id TEXT, result_url TEXT,
  placeholder_msg_id INTEGER,
  inline_message_id TEXT,                         -- Guest 模式:回填目标 inline 消息
  error TEXT, created_at INTEGER, finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gen_task ON generations(task_id);
CREATE INDEX IF NOT EXISTS idx_gen_status ON generations(status);

-- 审计日志
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id INTEGER, action TEXT, target_id INTEGER, detail TEXT, created_at INTEGER
);
