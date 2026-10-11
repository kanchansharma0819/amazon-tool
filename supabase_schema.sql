-- Run this once in Supabase Dashboard > SQL Editor.
-- This is a single-owner application protected by APP_ACCESS_PASSWORD at the web layer.
-- Keep the service-role key on the server only. No anon policies are created intentionally.
create extension if not exists pgcrypto;

create table if not exists public.sku_mappings (
    id uuid primary key default gen_random_uuid(),
    old_sku text not null,
    new_sku text not null,
    normalized_old_sku text not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint sku_mappings_old_sku_nonempty check (length(trim(old_sku)) > 0),
    constraint sku_mappings_new_sku_nonempty check (length(trim(new_sku)) > 0)
);

create index if not exists sku_mappings_old_sku_idx on public.sku_mappings (old_sku);
create index if not exists sku_mappings_new_sku_idx on public.sku_mappings (new_sku);
alter table public.sku_mappings enable row level security;

create or replace function public.set_sku_mappings_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_sku_mappings_updated_at on public.sku_mappings;
create trigger trg_sku_mappings_updated_at
before update on public.sku_mappings
for each row execute procedure public.set_sku_mappings_updated_at();

-- Do not add anon/authenticated policies unless you later implement Supabase Auth.
-- The backend accesses this table using SUPABASE_SERVICE_ROLE_KEY, which bypasses RLS.
