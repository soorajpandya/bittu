import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL = r'''
CREATE OR REPLACE FUNCTION fn_kyc_submit(
  p_merchant_id UUID,
  p_actor_user_id UUID
) RETURNS JSONB AS $func$
DECLARE
  v_profile merchant_kyc_profiles%ROWTYPE;
  v_owner_count INT;
  v_bank_count INT;
  v_pan_doc INT;
  v_addr_doc INT;
  v_bank_doc INT;
  v_missing TEXT[] := ARRAY[]::TEXT[];
BEGIN
  SELECT * INTO v_profile FROM merchant_kyc_profiles
    WHERE merchant_id = p_merchant_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'kyc profile not found' USING ERRCODE = 'P0002';
  END IF;
  IF v_profile.status NOT IN ('draft', 'rejected') THEN
    RAISE EXCEPTION 'cannot submit kyc from status %', v_profile.status USING ERRCODE = 'P0001';
  END IF;

  IF v_profile.legal_name IS NULL OR length(trim(v_profile.legal_name)) = 0 THEN
    v_missing := array_append(v_missing, 'legal_name');
  END IF;
  IF v_profile.business_type IS NULL THEN v_missing := array_append(v_missing, 'business_type'); END IF;
  IF v_profile.pan IS NULL OR length(v_profile.pan) <> 10 THEN
    v_missing := array_append(v_missing, 'pan');
  END IF;
  IF v_profile.contact_email IS NULL THEN v_missing := array_append(v_missing, 'contact_email'); END IF;
  IF v_profile.contact_phone IS NULL THEN v_missing := array_append(v_missing, 'contact_phone'); END IF;
  IF v_profile.registered_address = '{}'::jsonb THEN
    v_missing := array_append(v_missing, 'registered_address');
  END IF;

  SELECT COUNT(*) INTO v_owner_count FROM merchant_kyc_owners
    WHERE merchant_id = p_merchant_id;
  IF v_owner_count = 0 THEN v_missing := array_append(v_missing, 'owners'); END IF;

  SELECT COUNT(*) INTO v_bank_count FROM merchant_kyc_bank_accounts
    WHERE merchant_id = p_merchant_id AND is_primary = true;
  IF v_bank_count = 0 THEN v_missing := array_append(v_missing, 'primary_bank_account'); END IF;

  SELECT COUNT(*) INTO v_pan_doc FROM merchant_kyc_documents
    WHERE merchant_id = p_merchant_id AND doc_type = 'pan_card' AND status <> 'rejected';
  IF v_pan_doc = 0 THEN v_missing := array_append(v_missing, 'doc:pan_card'); END IF;

  SELECT COUNT(*) INTO v_addr_doc FROM merchant_kyc_documents
    WHERE merchant_id = p_merchant_id AND doc_type = 'address_proof' AND status <> 'rejected';
  IF v_addr_doc = 0 THEN v_missing := array_append(v_missing, 'doc:address_proof'); END IF;

  SELECT COUNT(*) INTO v_bank_doc FROM merchant_kyc_documents
    WHERE merchant_id = p_merchant_id AND doc_type = 'bank_proof' AND status <> 'rejected';
  IF v_bank_doc = 0 THEN v_missing := array_append(v_missing, 'doc:bank_proof'); END IF;

  IF array_length(v_missing, 1) > 0 THEN
    RAISE EXCEPTION 'kyc submission incomplete: %', array_to_string(v_missing, ',') USING ERRCODE = 'P0001';
  END IF;

  UPDATE merchant_kyc_profiles
     SET status = 'submitted', submitted_at = now(), rejection_reason = NULL,
         version = version + 1
   WHERE merchant_id = p_merchant_id
   RETURNING * INTO v_profile;

  INSERT INTO merchant_kyc_audit_events (merchant_id, event_type, from_status, to_status, actor_user_id)
  VALUES (p_merchant_id, 'profile.submitted', 'draft', 'submitted', p_actor_user_id);

  RETURN to_jsonb(v_profile);
END;
$func$ LANGUAGE plpgsql;
'''

async def main():
    await init_db_pool()
    try:
        async with get_connection() as c:
            await c.execute(SQL)
        print('OK fn_kyc_submit patched')
    finally:
        await close_db_pool()

asyncio.run(main())
