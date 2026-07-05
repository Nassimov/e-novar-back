-- Migration 038: Referral system overhaul
-- Run in Supabase SQL Editor

-- 1. Add permanent referral_code column to profiles
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS referral_code VARCHAR(30);
CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_referral_code ON profiles(referral_code)
  WHERE referral_code IS NOT NULL;

-- 2. Remove the spurious UNIQUE constraint on referrals.code so one code can
--    be reused by many referees. Each row is now one (referrer, referee) pair.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'referrals_code_key' AND contype = 'u'
  ) THEN
    ALTER TABLE referrals DROP CONSTRAINT referrals_code_key;
  END IF;
END$$;

-- 3. Add index on referrals.code for fast lookups
CREATE INDEX IF NOT EXISTS idx_referrals_code     ON referrals(code);
CREATE INDEX IF NOT EXISTS idx_referrals_referee  ON referrals(referee_id);

-- 4. Add referee_role column so we know which reward tier applies at
--    validation time without re-querying user_roles
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS referee_role VARCHAR(20) DEFAULT 'student';

-- 5. Drop old status "completed" alias — we use pending / registered / validated
--    (No-op if the enum doesn't exist; the application handles it via string)
