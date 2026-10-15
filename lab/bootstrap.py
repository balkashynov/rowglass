"""Create the dedicated demonstration tables and real PeerDB mirror, once."""

import psycopg2

SOURCE = (
    "host=127.0.0.1 port=55432 user=rowglass password=rowglass-local dbname=rowglass"
)
PEERDB = "host=127.0.0.1 port=59900 user=peerdb password=rowglass-local dbname=peerdb"


def main(peers_only=False):
    if not peers_only:
        with psycopg2.connect(SOURCE) as conn, conn.cursor() as cur:
            cur.execute("""CREATE TABLE public.orders (
          id bigint PRIMARY KEY, customer text NOT NULL, amount numeric(38,6),
          status text NOT NULL, metadata jsonb, updated_at timestamptz NOT NULL);
          CREATE TABLE public.payments (
          id bigint PRIMARY KEY, order_id bigint NOT NULL, amount numeric(38,6), state text NOT NULL);
          INSERT INTO orders SELECT i, 'customer-' || (i % 97), (i * 1.250001)::numeric(38,6),
          'pending', jsonb_build_object('region', CASE WHEN i%2=0 THEN 'ca' ELSE 'us' END),
          '2026-01-01 00:00:00+00' FROM generate_series(1,2000) i;
          CREATE PUBLICATION rowglass_demo FOR TABLE public.orders, public.payments;""")
    conn = psycopg2.connect(PEERDB)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE PEER IF NOT EXISTS rowglass_source FROM POSTGRES WITH
              (host='source',port='5432',user='rowglass',password='rowglass-local',database='rowglass');""")
            cur.execute("""CREATE PEER IF NOT EXISTS rowglass_target FROM CLICKHOUSE WITH
              (host='clickhouse',port='9000',user='rowglass',password='rowglass-local',database='rowglass',
               disable_tls='true',s3_path='s3://rowglass',access_key_id='rowglass-minio',
               secret_access_key='rowglass-minio-local',region='us-east-1',endpoint='http://minio:9000');""")
            cur.execute("""CREATE MIRROR rowglass_demo FROM rowglass_source TO rowglass_target
              WITH TABLE MAPPING (public.orders:orders,public.payments:payments)
              WITH (do_initial_copy=true,sync_interval=1,max_batch_size=10000,
                snapshot_num_rows_per_partition=1000,snapshot_max_parallel_workers=2);""")
            print(cur.statusmessage)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    main("--peers-only" in sys.argv)
