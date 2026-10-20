-- Briefing read aloud: one MP3 per briefing day, and a ledger of billed characters.
CREATE TABLE briefing_audio (
  date TEXT PRIMARY KEY REFERENCES briefings(date) ON DELETE CASCADE,
  voice_id TEXT NOT NULL,
  voice_name TEXT NOT NULL,
  model_id TEXT NOT NULL,
  characters INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  file TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE tts_usage (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  date TEXT NOT NULL,
  model_id TEXT NOT NULL,
  characters INTEGER NOT NULL,
  ok INTEGER NOT NULL
);
CREATE INDEX tts_usage_at ON tts_usage(at);
