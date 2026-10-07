# Extra trusted certificates (only for networks that inspect HTTPS)

Leave this folder empty unless the image build fails with errors such as
`UNABLE_TO_GET_ISSUER_CERT_LOCALLY`, `SELF_SIGNED_CERT_IN_CHAIN` or
`certificate verify failed`. Those mean a firewall or proxy on your network is
intercepting HTTPS (TLS inspection) and re-signing it with your organisation's
own root certificate.

To fix it, put that root certificate here as a PEM file ending in `.crt` or
`.pem`, then run `docker compose up -d --build` again. Every image build trusts
it automatically; nothing else needs to change.

Where to get it:

- **From IT.** Ask for the root CA certificate used for TLS/SSL inspection, in
  PEM (Base64) format.
- **From this machine**, if it already trusts it (for example `curl
  https://registry.npmjs.org` works on the host). On Debian/Ubuntu, copy the
  file IT installed:
  `cp /usr/local/share/ca-certificates/*.crt certs/`

Don't take a certificate from whatever the network happens to present (for
example by copying it out of an `openssl s_client` session). That would trust
an interceptor without checking it is really yours.

Certificate files here are ignored by git. They are public certificates, not
secrets, but they are specific to your network.
