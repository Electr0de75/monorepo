# CLAUDE.md - Govex DAO Monorepo Guide

## Project Overview

**Govex** is a DAO management platform built on the Sui blockchain using a Hanson-style futarchy mechanism for decision markets. Instead of voting, users trade on conditional markets to predict whether proposals should pass or fail.

### Tech Stack
- **Blockchain**: Sui (Move language)
- **Backend**: Node.js + Express + Prisma ORM
- **Frontend**: React 18 + Vite + SSR + TypeScript
- **Package Manager**: pnpm v9
- **Node Version**: 18.20.5 (see `.nvmrc`)

---

## Quick Start

```bash
# Install dependencies
nvm use && pnpm install

# Backend development
cd backend && pnpm dev:local

# Frontend development (separate terminal)
cd frontend && pnpm dev:local

# Run contract tests
cd contracts && sui move test
```

---

## Repository Structure

```
monorepo/
├── backend/              # Express API, event indexer, TWAP poller
├── frontend/             # React + Vite SSR application
├── contracts/            # Sui Move smart contracts (22 packages)
├── proposals/            # Governance proposals
├── research/             # Research documents
└── deployment-scripts/   # Deployment utilities
```

---

## Backend (`/backend`)

### Architecture - 3 Main Services

1. **API Service** (Port 3000)
   - Location: `/backend/server/index.ts`
   - REST API, health checks, OG image generation, AI review

2. **Event Indexer Service**
   - Location: `/backend/indexer/`
   - Listens to Sui blockchain events (DAOs, proposals, swaps, verifications)

3. **TWAP Poller Service**
   - Location: `/backend/poller/twapPoller.ts`
   - Polls time-weighted average prices for oracle data

### Key Commands

```bash
pnpm dev              # Run all 3 services
pnpm dev:local        # Dev with NETWORK=devnet
pnpm api:dev          # API only
pnpm indexer          # Indexer only
pnpm poll:dev         # Poller only
pnpm build            # TypeScript compile + Prisma generate
```

### Database Commands

```bash
pnpm db:reset:dev     # Reset local SQLite
pnpm db:setup:dev     # Create/migrate local DB
pnpm db:setup:mainnet # Deploy mainnet migrations
```

### Database Schemas
- `schema.prisma` - Default (local dev)
- `schema.mainnet.prisma` - Production
- `schema.testnet-dev.prisma` - Testnet

---

## Frontend (`/frontend`)

### Directory Structure

```
src/
├── routes/           # Page components (auto-routed)
├── components/       # Reusable UI components
├── hooks/            # Custom React hooks (useSuiTransaction, useTokenBalance)
├── mutations/        # React Query mutations
├── utils/            # Utilities (trade calculations, validation)
├── constants.ts      # Network config, API endpoints
└── App.tsx           # Main router
```

### Key Commands

```bash
pnpm dev              # SSR dev server on :5173
pnpm dev:local        # Local with API at localhost:3000
pnpm build-frontend   # Build client + server bundles
pnpm lint             # ESLint + Prettier check
pnpm lint:fix         # Auto-fix lint issues
pnpm test             # Run Vitest
```

---

## Smart Contracts (`/contracts`)

### 22 Core Packages

| Package | Purpose |
|---------|---------|
| `futarchy_core` | Core futarchy logic, fees, version management |
| `futarchy_dao` | DAO creation and management |
| `futarchy_factory` | DAO factory for permissionless creation |
| `futarchy_markets` | Conditional AMM markets |
| `futarchy_lifecycle` | Proposal state machine |
| `futarchy_oracle` | TWAP oracle for price feeds |
| `futarchy_governance_actions` | Governance implementations |
| `futarchy_vault` | Treasury management |

### Commands

```bash
sui move build                    # Build all packages
sui move test                     # Run tests
sui move test --path ./package    # Test specific package
```

### Critical Patterns

1. **BCS Serialization**: Actions are serialized as bytes for deferred execution
2. **Quantum Liquidity**: 1 spot token → 1 conditional token per outcome
3. **Frontend Control**: Frontend determines action type parameters
4. **Write-Through Oracle**: Write observation before reading

> **IMPORTANT**: See `/contracts/CLAUDE.md` for detailed contract patterns

---

## Code Quality

### Linting & Formatting

```bash
# Frontend
pnpm lint:fix                     # ESLint + Prettier auto-fix
pnpm prettier:fix                 # Format only

# Contracts
npx prettier -w ./sources/**/*.move
```

### Testing

```bash
# Frontend
cd frontend && pnpm test

# Contracts
cd contracts && sui move test
```

---

## Environment Variables

### Backend (`.env`)

```env
DATABASE_URL=file:./dev.db        # Local SQLite
SUI_PRIVATE_KEY=base64_key
SUI_RPC_URL=https://fullnode.testnet.sui.io:443
NETWORK=testnet
PACKAGE_ID=0x...
FEE_MANAGER_ID=0x...
PORT=3000
```

### Frontend

```env
VITE_API_URL=http://localhost:3000/
VITE_NETWORK=testnet
```

---

## Conventions

### Naming
- **Files**: camelCase.ts (TS), snake_case.move (Move)
- **Components**: PascalCase
- **Functions**: camelCase
- **Constants**: UPPER_SNAKE_CASE

### API Response Format

```typescript
// Success
res.json({ data, message?: string })

// Error
res.status(code).json({ error: string })
```

### Import Patterns
- Frontend uses `@/` alias for src/
- Always use `import type` for TypeScript-only imports

---

## Deployment

### Railway Services
1. **API** - Port 3000
2. **Bot** - Proposal state machine
3. **Indexer** - Event listener
4. **Poller** - TWAP oracle
5. **Frontend** - React app

### Config Files
- `railway.api.json`, `railway.bot.json`, `railway.indexer.json`
- `nixpacks.toml` - Build configuration

---

## Security Considerations

### Fixed Vulnerabilities
- Path traversal - Validated against whitelist
- XSS - Using `res.json()` not `res.send()`
- Rate limiting - express-rate-limit on OG endpoints
- Header leakage - X-Powered-By disabled

### Best Practices
- Always convert BigInt to string for JSON responses
- Validate image URLs before rendering
- Use generic error messages (no sensitive info)

---

## Critical Reminders for AI Assistants

1. **Never delete Move modules** without explicit approval
2. **Read code before modifying** - Understand existing patterns first
3. **BCS serialization is intentional** - Enables pre-DAO action storage
4. **Quantum liquidity model** - Conditional markets exist in parallel
5. **BigInt serialization** - Always convert to string for JSON
6. **Environment awareness** - Check NETWORK var for target network
7. **Rate limiting required** - OG image endpoint needs protection
8. **Atomic operations** - Sui has no reentrancy risk

---

## Common Tasks

### Add New API Endpoint
1. Create route in `/backend/server/routes/`
2. Import in `/backend/server/index.ts`
3. Return via `res.json()`

### Add New Frontend Page
1. Create `.tsx` in `/frontend/src/routes/`
2. Auto-routes from filename (Vite Pages plugin)

### Add Database Model
1. Update `/backend/prisma/schema.prisma`
2. Run `pnpm prisma migrate dev --name add_model`

### Add Smart Contract Module
1. Create directory in `/contracts/`
2. Create `Move.toml` and `sources/`
3. Test with `sui move test --path ./module`

---

## Documentation References

- `/contracts/CLAUDE.md` - Contract-specific patterns (CRITICAL)
- `/contracts/FUTARCHY_ACTIONS.md` - Action documentation
- `/RAILWAY_DEPLOYMENT_GUIDE.md` - Deployment procedures
- `/SECURITY_ISSUES.md` - Security fixes
- `/CONTRIBUTING.md` - Contribution guidelines
