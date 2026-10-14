-- feedback-1 (A3): X posts no longer belong to Today stories; they get their own page.
-- Detach every item from an x_person source, delete the stories that this leaves empty, and
-- rescore the rest exactly as Store.recompute_story_score does (best signal + 0.5 per extra
-- source; a changed lead item flags the story for rewriting).

CREATE TEMP TABLE _x_touched AS
    SELECT DISTINCT i.story_id AS id
    FROM items i JOIN sources s ON s.id = i.source_id
    WHERE i.story_id IS NOT NULL AND s.kind = 'x_person';

UPDATE items SET story_id = NULL
WHERE story_id IS NOT NULL
  AND source_id IN (SELECT id FROM sources WHERE kind = 'x_person');

DELETE FROM stories
WHERE id IN (SELECT id FROM _x_touched)
  AND NOT EXISTS (SELECT 1 FROM items i WHERE i.story_id = stories.id);

-- Every right-hand side sees the row's old values, so lead_item_id is compared before it changes.
UPDATE stories SET
    needs_text = CASE WHEN lead_item_id IS NOT (
            SELECT i.id FROM items i WHERE i.story_id = stories.id
            ORDER BY COALESCE(i.signal, -1) DESC, i.id ASC LIMIT 1)
        THEN 1 ELSE needs_text END,
    text_attempts = CASE WHEN lead_item_id IS NOT (
            SELECT i.id FROM items i WHERE i.story_id = stories.id
            ORDER BY COALESCE(i.signal, -1) DESC, i.id ASC LIMIT 1)
        THEN 0 ELSE text_attempts END,
    lead_item_id = (
        SELECT i.id FROM items i WHERE i.story_id = stories.id
        ORDER BY COALESCE(i.signal, -1) DESC, i.id ASC LIMIT 1),
    score = (
        SELECT COALESCE(MAX(i.signal), 0) + 0.5 * (COUNT(DISTINCT i.source_id) - 1)
        FROM items i WHERE i.story_id = stories.id)
WHERE id IN (SELECT id FROM _x_touched);

DROP TABLE _x_touched;
