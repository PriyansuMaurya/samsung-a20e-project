# Public access plan for small.maurya.ie

Status: not configured or tested. The homepage was reachable on the LAN, but the deployed status endpoint still needs the corrected `status.py`, and reboot startup still needs testing. `small.maurya.ie` already resolves to existing addresses; repointing it changes where that name goes. Inspect and record the existing Cloudflare DNS records before changing them so they can be restored.

The domain uses Cloudflare nameservers. A remotely managed Cloudflare Tunnel can publish `https://small.maurya.ie` to `http://127.0.0.1:8080` on the phone without router port forwarding. Termux packages `cloudflared`. The existing Nginx allow rule permits loopback, so a connector on the phone can reach it. Public visitors will be able to view the website and dashboard information. Before publishing, review what those pages reveal.

The account owner needs to sign in to Cloudflare, create a remotely managed tunnel, and configure a published-application route with hostname `small.maurya.ie` and service `http://127.0.0.1:8080`. Existing conflicting A, AAAA or CNAME records must be handled in the Cloudflare dashboard; preserve their prior values. Keep the tunnel token private: anyone holding it can run a connector. Do not paste it into chat or shared storage. Termux provides `cloudflared` via `pkg install cloudflared`; its boot integration and private credential storage should be configured and tested on the phone once account access and an unlocked device are available.

Verify HTTPS and the page and dashboard from a device outside the local Wi-Fi, then restart the phone, unlock it and repeat. Record resource usage after adding the tunnel. Android battery settings may need to allow Termux and Termux:Boot to run in the background. The app can still be stopped by Android; no uptime guarantee has been established.

References: [Cloudflare Tunnel setup](https://developers.cloudflare.com/tunnel/get-started/), [tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/), [Termux cloudflared package](https://github.com/termux/termux-packages/blob/master/packages/cloudflared/build.sh).
