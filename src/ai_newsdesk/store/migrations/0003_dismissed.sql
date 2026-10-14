-- feedback-1 (dismiss): the owner can hide a Today story or an X post ("done with this").
-- NULL = visible; otherwise the UTC ISO-8601 time it was dismissed. Dismissing never touches
-- feedback, source weights or the taste model.

ALTER TABLE stories ADD COLUMN dismissed_at TEXT;
ALTER TABLE items ADD COLUMN dismissed_at TEXT;

CREATE INDEX ix_stories_dismissed ON stories (dismissed_at) WHERE dismissed_at IS NOT NULL;
CREATE INDEX ix_items_dismissed ON items (dismissed_at) WHERE dismissed_at IS NOT NULL;
