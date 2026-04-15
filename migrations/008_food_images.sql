-- Migration 008: Food Image Pipeline
-- Global food image cache shared across all restaurants

-- ── food_images table (global, not per-tenant) ──
CREATE TABLE IF NOT EXISTS food_images (
    name            TEXT PRIMARY KEY,           -- normalized food name
    image_url       TEXT NOT NULL,              -- legacy / same as original
    image_original_url TEXT NOT NULL,           -- full size (1024)
    image_512_url   TEXT NOT NULL,              -- medium (512)
    image_256_url   TEXT NOT NULL,              -- thumbnail (256)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_food_images_created_at ON food_images (created_at DESC);

-- ── Add food_image_name FK to items table ──
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'items' AND column_name = 'food_image_name'
    ) THEN
        ALTER TABLE items ADD COLUMN food_image_name TEXT NULL
            REFERENCES food_images(name) ON DELETE SET NULL;
    END IF;
END $$;

-- ── Auto-update updated_at trigger ──
CREATE OR REPLACE FUNCTION update_food_images_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_food_images_updated_at ON food_images;
CREATE TRIGGER trg_food_images_updated_at
    BEFORE UPDATE ON food_images
    FOR EACH ROW
    EXECUTE FUNCTION update_food_images_updated_at();

-- ── Supabase Storage bucket (run manually or via dashboard) ──
-- Create a public bucket named 'food-images' in Supabase Storage dashboard
-- Or via SQL: INSERT INTO storage.buckets (id, name, public) VALUES ('food-images', 'food-images', true);

-- ── RLS: food_images is global read, service-role write ──
ALTER TABLE food_images ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS food_images_select_all ON food_images;
CREATE POLICY food_images_select_all ON food_images
    FOR SELECT USING (true);

DROP POLICY IF EXISTS food_images_insert_service ON food_images;
CREATE POLICY food_images_insert_service ON food_images
    FOR INSERT WITH CHECK (true);

DROP POLICY IF EXISTS food_images_update_service ON food_images;
CREATE POLICY food_images_update_service ON food_images
    FOR UPDATE USING (true);
