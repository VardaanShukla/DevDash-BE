# Host the team server on a server

For a team server that stays up at your own address (a small VPS is plenty). The server keeps
accounts, organisations, teams and the explanations people share; never code.

1. Point a domain at the server: an A record, like `teams.example.com` → the server's IP.
2. Open ports 80 and 443 in the server's firewall.
3. Copy the DevDash-BE folder there, then in it:

   ```sh
   echo "DOMAIN=teams.example.com" > deploy/.env
   docker compose -f deploy/docker-compose.yml up -d --build
   docker compose -f deploy/docker-compose.yml logs devdash
   ```

4. The log shows a **setup code**. Open https://teams.example.com, make an account, and enter the
   code under You → Server to become the server's admin (where you can close sign-ups).

Everyone then signs up there, makes or joins an organisation, and links their own DevDash from
Settings → Team server.

Without Docker, on the server itself: install DevDash and [Caddy](https://caddyserver.com/docs/install),
then `devdash host --tunnel domain --domain teams.example.com` (or the installer's 5 Team server
→ Host → Your domain). Run it as a service to keep it up after you log out.

Its data is in the `devdash-data` volume (`~/.devdash/team` without Docker): back it up.
