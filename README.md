# Attention Buddy

**Attention Buddy is an AI-powered attention management assistant for solo founders and small businesses.**

Running a business means dealing with a constant stream of emails, enquiries, requests and problems—but not every message deserves the founder's attention. Attention Buddy goes beyond summarizing an inbox by determining **what actually requires the founder to act**.

Incoming messages are analyzed together with relevant business knowledge and founder preferences. Attention Buddy then routes each message based on the level of attention required:

* **AUTO_HANDLE** — routine enquiries that can be handled with minimal founder involvement.
* **APPROVAL_REQUIRED** — AI prepares the work, but a business decision still requires the founder.
* **ESCALATE_NOW** — urgent or high-risk situations requiring immediate founder attention.

Attention Buddy prepares context-aware responses so founders don't have to start from scratch. When intervention is required, the founder can see **why they are needed**, review the specific decisions required, edit the AI-prepared draft, approve it, and send the response.

## How It Works

```text
Incoming Gmail
      ↓
Business Knowledge + Founder Memory
      ↓
Atlas — Attention Decision Engine
      ↓
┌─────────────────────────────────────┐
│ AUTO_HANDLE                         │
│ APPROVAL_REQUIRED                   │
│ ESCALATE_NOW                        │
└─────────────────────────────────────┘
      ↓
Clio — Communication Agent
      ↓
Prepared Response
      ↓
Founder Review / Edit / Approval
      ↓
Gmail Response
```

For urgent `ESCALATE_NOW` cases, **Telegram notifications** alert the founder so high-priority situations are not buried in the inbox.

## Founder Learning Loop

Attention Buddy is designed to become more personalized over time.

Founder actions—such as editing AI-generated responses, approving decisions and handling escalated cases—can feed into a **learning loop** that captures recurring preferences and decision patterns.

Rather than treating every interaction as an isolated email, Attention Buddy gradually builds a better understanding of **how the founder prefers the business to operate**, allowing future recommendations and responses to become increasingly relevant.

## Key Features

* Gmail inbox integration
* AI-powered message prioritization
* Business Knowledge retrieval
* Founder Memory
* Three-level attention routing: `AUTO_HANDLE`, `APPROVAL_REQUIRED`, `ESCALATE_NOW`
* Context-aware response generation
* Founder decision summaries
* Draft editing and approval
* Gmail response workflow
* Telegram alerts for urgent escalations
* Founder preference learning loop
* Persistent decision and communication states

## Built with WorkBuddy

WorkBuddy was used to design and prototype Attention Buddy's **multi-agent architecture**. Specialized AI experts were created for message understanding, attention prioritization, response planning and communication, with structured handoffs between agents.

The resulting architecture separates **attention decisions from communication**, allowing the system to determine whether the founder is actually needed before deciding how a response should be prepared.

## Why Attention Buddy?

Most AI inbox tools help users **process more messages**.

Attention Buddy asks a different question:

> **Does this actually need your attention?**

The goal is not simply to automate email. It is to protect the founder's limited attention by handling routine work, preparing decisions that require judgment, and immediately surfacing situations where human involvement matters most.
