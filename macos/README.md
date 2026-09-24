# NexusAI macOS Edge Agent Installer

This directory defines the production macOS installer pipeline.

The intended customer artifact is a signed and notarized `NexusAI-Edge-Agent-macOS.pkg`.

The GitHub Actions workflow builds the package on macOS. When Apple Developer credentials are configured, it signs the package with a Developer ID Installer certificate, submits it to Apple for notarization, and staples the ticket.

Required repository secrets:
- APPLE_CERTIFICATE_P12_BASE64
- APPLE_CERTIFICATE_PASSWORD
- APPLE_INSTALLER_IDENTITY
- APPLE_ID
- APPLE_TEAM_ID
- APPLE_APP_PASSWORD

Without those secrets, the workflow only produces an unsigned internal-test package, so macOS may still show an unidentified-developer warning.

The package also configures a LaunchAgent so the Edge Agent automatically starts after the user's Mac login.
