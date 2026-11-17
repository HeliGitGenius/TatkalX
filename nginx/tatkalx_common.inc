# Shared proxy behaviour for /api (included by both nginx.conf and nginx.local.conf)

proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

# Fail fast and fail over. POSTs are retried on another node ONLY because every booking
# carries an Idempotency-Key: a retry can never create a second booking.
proxy_connect_timeout 1s;
proxy_send_timeout 15s;
proxy_read_timeout 20s;
proxy_next_upstream error timeout http_502 http_503 non_idempotent;
proxy_next_upstream_tries 3;
proxy_next_upstream_timeout 10s;

add_header X-Upstream-Addr $upstream_addr always;
client_max_body_size 64k;
