# Security

Do not attach bot tokens, client configuration files, private keys, database
files or QR images to issues. Use GitHub private vulnerability reporting for
security issues. Public issues should contain only sanitized diagnostics.

This alpha supports a single trusted Telegram owner. Root-installed code,
sudoers and configuration must not be writable by the bot account. Bot token
compromise requires revocation through BotFather; client key compromise requires
revoking or reissuing that peer. The manager is not a multi-tenant billing system.
