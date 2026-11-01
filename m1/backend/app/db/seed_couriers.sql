-- CourierLedger M1 -- seed the global courier reference list (v0.1 C.2).
--
-- Codes ONLY -- no rates, no statement layouts, no fee semantics.
-- Courier codes come from the M1 kickoff brief (Steadfast, Pathao,
-- Paperfly, RedX, eCourier). Adding a code is a migration; this script
-- only inserts the five brief-named codes if they are absent.
-- Apply once per database (dev/app/test scratch DBs), as the owner
-- role. Idempotent: ON CONFLICT DO NOTHING.
INSERT INTO public.couriers (id, code, name, is_active) VALUES
  (gen_random_uuid(), 'steadfast', 'Steadfast', true),
  (gen_random_uuid(), 'pathao', 'Pathao', true),
  (gen_random_uuid(), 'paperfly', 'Paperfly', true),
  (gen_random_uuid(), 'redx', 'RedX', true),
  (gen_random_uuid(), 'ecourier', 'eCourier', true)
ON CONFLICT (code) DO NOTHING;
