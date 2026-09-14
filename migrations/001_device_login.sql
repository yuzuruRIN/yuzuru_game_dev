-- ============================================================
-- 001_device_login.sql
-- ระบบล็อกอินหน้าแรก: ผูกเครื่อง + OTP + claim code
--
-- หมายเหตุสำคัญเรื่อง email:
--   member_list ใช้ email เป็น unique key (upsert on_conflict="email")
--   แต่ข้อมูลเดิมมีทั้งตัวพิมพ์เล็ก/ใหญ่ปนกัน (ดู _get_member ที่ต้อง
--   fallback ไป ilike) ตารางใหม่ทั้งหมดจึงเก็บ email เป็น "ตัวพิมพ์เล็ก
--   ล้วน" เสมอ และฝั่งแอปต้อง .lower().strip() ก่อนเขียนทุกครั้ง
-- ============================================================


-- ------------------------------------------------------------
-- 1) ขยาย member_list
-- ------------------------------------------------------------
alter table member_list add column if not exists source         text    not null default 'patreon';
alter table member_list add column if not exists discord_id     text;
alter table member_list add column if not exists email_verified boolean not null default false;
alter table member_list add column if not exists device_slots   smallint not null default 2;

comment on column member_list.source         is 'patreon | discord | manual';
comment on column member_list.discord_id     is 'Discord user ID (ตัวเลข) - เสถียรกว่าชื่อที่เปลี่ยนได้';
comment on column member_list.email_verified is 'true เมื่อผ่าน OTP อย่างน้อยหนึ่งครั้ง';
comment on column member_list.device_slots   is 'จำนวนเครื่องที่ผูกพร้อมกันได้';

-- ทำเครื่องหมายผู้โดเนทเดิมที่ยังใช้อีเมลปลอม
update member_list
   set source = 'discord'
 where email like '%@donator.discord';

-- Dev ให้ slot เยอะหน่อยไว้เทสต์หลายเครื่อง
update member_list
   set device_slots = 10
 where email in ('lxpetitprixce@gmail.com', 'devthelastyear@yuzuru.rin');


-- ------------------------------------------------------------
-- 2) devices - เครื่องที่ผูกกับอีเมล
-- ------------------------------------------------------------
create table if not exists devices (
    id           bigint generated always as identity primary key,
    email        text        not null,
    hwid_hash    text        not null,   -- sha256(salt + hwid ดิบ) ทำจากฝั่งเกม
    platform     text,                   -- windows | mac | android
    label        text,                   -- ผู้เล่นตั้งเอง เช่น "คอมที่บ้าน"
    activated_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    released_at  timestamptz,            -- null = ยังใช้อยู่
    released_by  text,                   -- self | dev
    constraint devices_email_hwid_key unique (email, hwid_hash)
);

-- นับ slot ที่ใช้อยู่ได้เร็ว
create index if not exists devices_active_idx
    on devices (email) where released_at is null;

-- ใช้ตรวจ cooldown การปลดเครื่องด้วยตัวเอง
create index if not exists devices_released_idx
    on devices (email, released_at desc) where released_at is not null;


-- ------------------------------------------------------------
-- 3) otp_codes - รหัสยืนยันทางอีเมล
-- ------------------------------------------------------------
create table if not exists otp_codes (
    id          bigint generated always as identity primary key,
    email       text        not null,
    code_hash   text        not null,   -- เก็บ hash เท่านั้น ไม่เก็บเลข 6 หลักตรง ๆ
    purpose     text        not null,   -- activate | release
    hwid_hash   text,                   -- ผูกกับเครื่องที่ขอ กันเอา OTP ไปใช้เครื่องอื่น
    expires_at  timestamptz not null,
    attempts    smallint    not null default 0,
    consumed_at timestamptz,
    created_at  timestamptz not null default now()
);

create index if not exists otp_codes_lookup_idx
    on otp_codes (email, purpose, created_at desc);

-- ใช้ rate limit: นับว่าขอ OTP ไปกี่ครั้งในช่วงเวลาหนึ่ง
create index if not exists otp_codes_ratelimit_idx
    on otp_codes (email, created_at desc);


-- ------------------------------------------------------------
-- 4) claim_codes - สำหรับผู้โดเนทตรงผ่าน Discord
-- ------------------------------------------------------------
create table if not exists claim_codes (
    code             text primary key,   -- เช่น TLY-7K2M-9QX4
    tier             text        not null default 'Donator',
    expires_at       timestamptz not null,
    note             text,               -- ชื่อ Discord / วันที่โดเนท ไว้ให้ dev ดู
    discord_id       text,
    claimed_by_email text,
    claimed_at       timestamptz,
    created_at       timestamptz not null default now(),
    created_by       text
);

create index if not exists claim_codes_unclaimed_idx
    on claim_codes (expires_at) where claimed_by_email is null;


-- ------------------------------------------------------------
-- 5) auth_log - ไว้ไล่ดูตอนมีคนทักมาว่าเข้าไม่ได้
-- ------------------------------------------------------------
create table if not exists auth_log (
    id         bigint generated always as identity primary key,
    email      text,
    event      text not null,   -- otp_sent | otp_failed | activated | denied_no_slot | released | ...
    hwid_hash  text,
    detail     jsonb,
    created_at timestamptz not null default now()
);

create index if not exists auth_log_email_idx on auth_log (email, created_at desc);


-- ------------------------------------------------------------
-- 6) RLS - ทุกตารางเข้าถึงผ่าน service key จากหลังบ้านเท่านั้น
--    (เปิด RLS โดยไม่สร้าง policy = ปิดตายสำหรับ anon/authenticated)
-- ------------------------------------------------------------
alter table devices     enable row level security;
alter table otp_codes   enable row level security;
alter table claim_codes enable row level security;
alter table auth_log    enable row level security;
