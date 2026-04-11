server {
    server_name api.bittupos.com;

    # ── Request Size (menu image uploads) ──
    client_max_body_size 10M;

    # ── Timeouts ──
    proxy_connect_timeout 10s;
    proxy_send_timeout 30s;
    proxy_read_timeout 120s;   # AI menu scan can take up to 2 min

    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/api.bittupos.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.bittupos.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

server {
    if ($host = api.bittupos.com) {
        return 301 https://$host$request_uri;
    }

    listen 80;
    server_name api.bittupos.com;
    return 404;
}
