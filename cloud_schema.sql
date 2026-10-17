-- Run this in the Supabase SQL Editor for optional online snapshot sync.
-- The application uses the service-role key only on the Python backend.
create table if not exists public.business_os_accounts (
  email text primary key,
  snapshot jsonb not null,
  updated_at timestamptz not null default now()
);

alter table public.business_os_accounts enable row level security;
-- Do not add public read/write policies. Server-side service-role access bypasses RLS.
