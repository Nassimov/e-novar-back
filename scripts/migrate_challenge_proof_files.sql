-- Migration: add missing columns + challenge_proof_files table
-- Run this once in the Supabase SQL editor.

-- 1. Add missing columns to challenge_participations
alter table public.challenge_participations
  add column if not exists proof_message text,
  add column if not exists deadline_at   timestamptz;

-- 2. Create challenge_proof_files (one row per uploaded file)
create table if not exists public.challenge_proof_files (
  id               uuid        primary key default gen_random_uuid(),
  participation_id uuid        not null references public.challenge_participations(id) on delete cascade,
  storage_path     text        not null,
  original_filename text       not null,
  mime_type        text        not null,
  file_size_bytes  integer     not null default 0,
  sort_order       integer     not null default 0,
  uploaded_at      timestamptz not null default now()
);

create index if not exists idx_cpf_participation
  on public.challenge_proof_files(participation_id);

-- RLS: admins can read all; participants can read their own
alter table public.challenge_proof_files enable row level security;

create policy "Admins read proof files"
  on public.challenge_proof_files for select
  to authenticated
  using (
    exists (
      select 1 from public.profiles
      where profiles.id = auth.uid() and profiles.role = 'admin'
    )
  );

create policy "Participants read own proof files"
  on public.challenge_proof_files for select
  to authenticated
  using (
    exists (
      select 1 from public.challenge_participations cp
      where cp.id = challenge_proof_files.participation_id
        and cp.user_id = auth.uid()
    )
  );
