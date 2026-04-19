# Security Policy

## ⚠️ Before You Report

LambdaForge is an open-source algorithmic trading framework that **runs in your AWS account under your Alpaca credentials**. The authors do not host, operate, or have visibility into any user's deployed instance.

That means:

- **Credential compromise on your deployment** (e.g., exposed SSM parameters, leaked IAM keys) is a you-problem to remediate — rotate keys, check your AWS CloudTrail logs, revoke Alpaca API keys at https://app.alpaca.markets.
- **Losses from buggy strategy code** are not a security issue — they're a risk management issue. Always paper-trade first.

What **is** in scope for security reports: vulnerabilities in this repo's code that could allow an attacker to:

- Execute arbitrary code in a LambdaForge deployment
- Exfiltrate SSM parameters, AWS credentials, or Alpaca keys via a bug in LambdaForge (e.g., SSRF, log injection, unsafe deserialization)
- Trick a maintainer or contributor via a malicious PR
- Compromise the LambdaForge CI/CD pipeline or release artifacts
- Bypass the kill-switch or safety controls documented in `ARCHITECTURE.md`

## Supported Versions

LambdaForge uses rolling releases — only the latest tagged release on `main` receives security fixes.

| Version       | Supported          |
| ------------- | ------------------ |
| latest `main` | :white_check_mark: |
| older tags    | :x:                |

## Reporting a Vulnerability

**Please do not report security vulnerabilities via public GitHub issues.**

Instead, use GitHub's private vulnerability reporting:

1. Go to the repo's **Security** tab
2. Click **Report a vulnerability**
3. Fill in the form with:
   - A clear description of the vulnerability
   - Steps to reproduce (ideally a minimal PoC)
   - Affected files / functions / commit SHA
   - Potential impact on a deployed bot
   - Your suggested fix (if any)

### What to expect

- **Acknowledgement:** within 72 hours
- **Triage:** within 7 days (severity assessment + CVE if warranted)
- **Fix timeline:**
  - Critical (RCE, credential exfiltration): patched and released within 7 days
  - High (privilege escalation, kill-switch bypass): patched within 14 days
  - Medium / Low: patched in the next scheduled release

### Disclosure

Once a fix is released, the reporter will be credited in the release notes (unless they prefer anonymity). We follow coordinated disclosure — please do not publish details until the fix is live and users have had 7 days to update.

## Security Best Practices for Operators

If you run LambdaForge in your own AWS account:

1. **Never commit `.env`, `samconfig.toml`, `iam-deployer-policy.json`, or `iam-ops-policy.json`** — these are all gitignored. Verify with `git check-ignore -v <file>` before every push.
2. **Rotate Alpaca API keys every 90 days.** Automate via SSM.
3. **Enable MFA on your Alpaca account and AWS root account.** No exceptions.
4. **Use the kill-switch** (`aws ssm put-parameter --name /stock-bot/kill-switch --value kill`) the moment anything looks wrong. Investigate after, not before.
5. **Review the scoped IAM policies** (`iam-deployer-policy.template.json`, `iam-ops-policy.template.json`) before applying — don't grant broader permissions than you need.
6. **Monitor your CloudWatch billing alarms.** If a bug puts you in a Lambda hot-loop, you want to know before the AWS bill does.
7. **Always paper-trade a strategy for 2+ weeks before switching to live.**

## Scope Limitations

This security policy covers only the code in this repository. It does **not** cover:

- Bugs in Alpaca's API or SDK (report to Alpaca)
- Bugs in AWS services or the AWS SDK (report to AWS)
- Bugs in Python or third-party libraries (report upstream, but flag here if we should pin a safer version)
- Social engineering attacks targeting individual operators
