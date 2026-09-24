from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("meetings", "0001_initial")]
    operations = [
        migrations.RunSQL(
            sql="""
        ALTER TABLE meetings_meeting ADD CONSTRAINT reservation_range_valid
        CHECK (NOT reservation_active OR (
            NOT isempty(reservation) AND NOT lower_inf(reservation) AND NOT upper_inf(reservation)
            AND lower_inc(reservation) AND NOT upper_inc(reservation)
            AND lower(reservation) <= start_at AND upper(reservation) >= end_at
        ));
        CREATE FUNCTION gateway_room_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.username IS DISTINCT FROM OLD.username AND EXISTS
                (SELECT 1 FROM meetings_meeting WHERE room_id = OLD.id) THEN
                RAISE EXCEPTION 'Room account identity with meeting history is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER room_identity_guard BEFORE UPDATE ON rooms_room
        FOR EACH ROW EXECUTE FUNCTION gateway_room_identity_guard();
        """,
            reverse_sql="""
        DROP TRIGGER room_identity_guard ON rooms_room;
        DROP FUNCTION gateway_room_identity_guard();
        ALTER TABLE meetings_meeting DROP CONSTRAINT reservation_range_valid;
        """,
        )
    ]
