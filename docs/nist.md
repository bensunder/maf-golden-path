# NIST frameworks in the console

The console's **Security** page maps the agent's running controls to four NIST references:

| Framework | What's mapped |
|---|---|
| [AI RMF 1.0 (AI 100-1)](https://doi.org/10.6028/NIST.AI.100-1) | MAP, MEASURE and MANAGE subcategories on oversight, testing, security, privacy, transparency and monitoring |
| [AI 600-1, Generative AI Profile](https://doi.org/10.6028/NIST.AI.600-1) | Risks: confabulation, data privacy, human-AI configuration, information integrity, information security, value chain |
| [Cybersecurity Framework 2.0](https://doi.org/10.6028/NIST.CSWP.29) | PROTECT (identity and access, data security, platform security, resilience) and DETECT outcomes |
| [SP 800-53 Rev. 5](https://doi.org/10.6028/NIST.SP.800-53r5) | Controls in AC, AU, CM, IA, SA, SC and SI |

For each reference the console shows whether it's **Supported**, **Partly**, **Not in force** or has **No evidence here**. The evidence is the controls that support it, with their live status. For example, SP 800-53 **SC-23 Session Authenticity** is backed by *Signed requests only*, and **AC-3(2) Dual Authorization** by *Human approval*.

## Where the statuses come from

Statuses aren't typed in; they're read from the running agent:

- its middleware stack (prompt-injection checks, PII redaction, tool policy, output scanning, token budget, approvals);
- its settings (sign-in, platform signing, delegation, session store, telemetry sinks, content capture);
- its latest quality gate report (pass rate, and whether it was a live run).

If a control is switched off, every reference it backs changes with it.

## What this is and isn't

It's a mapping to start an assessment from, with evidence an assessor can check. It isn't a certification or an assessment. Organizational controls, such as policies, training, incident response and supplier agreements, are outside what an agent can show. The mapping lives in `console/src/lib/nist.ts`. Review it with your security team, and change it there if your organization reads a reference differently.
