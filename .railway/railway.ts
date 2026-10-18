// Railway infrastructure for Kiki. Review changes with `railway config plan`,
// then apply with `railway config apply`.
import { defineRailway, github, postgres, preserve, project, service, volume } from "railway/iac";

const REPO = "carltonwu/kiki";
// Railway's US East (Ashburn, Virginia). Keep every service next to Postgres.
const REGION = "iad";

export default defineRailway(() => {
  const db = postgres("postgres", { region: REGION });
  const dbVolume = volume("postgres-volume", { sizeMB: 5000, region: REGION });

  const agent = service("agent", {
    source: github(REPO, { branch: "main", checkSuites: true }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "services/agent/Dockerfile",
      watchPatterns: ["services/agent/**"],
    },
    regions: { [REGION]: { replicas: 1 } },
    deploy: {
      healthcheckPath: "/health",
      healthcheckTimeout: 60,
      sleepApplication: true,
      restartPolicyType: "ON_FAILURE",
      drainingSeconds: 30,
    },
    env: {
      DATABASE_URL: db.env.DATABASE_URL,
      PORT: "8081",
      AGENT_HOST_TOKEN: "${{shared.AGENT_HOST_TOKEN}}",
      AGENT_HOST_MAX_CONCURRENT_JOBS: "2",
      AGENT_HOST_JOB_TIMEOUT_SECS: "120",
      // Chat and embeddings via OpenRouter; without it the host returns 501.
      OPENROUTER_CHAT_API_KEY: preserve(),
    },
  });

  const ingester = service("ingester", {
    // No rootDirectory: the build needs the whole Cargo workspace.
    // checkSuites: deploy only after GitHub Actions checks pass (.github/workflows/ci.yml).
    source: github(REPO, { branch: "main", checkSuites: true }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "services/ingester/Dockerfile",
      // Only redeploy when something the ingester is built from changes.
      watchPatterns: ["services/ingester/**", "Cargo.toml", "Cargo.lock"],
    },
    regions: { [REGION]: { replicas: 1 } },
    deploy: {
      // Migrations run once per deploy, before the new instance starts. A failure
      // stops the deploy and leaves the old instance running.
      preDeployCommand: ["ingester migrate"],
      healthcheckPath: "/health",
      healthcheckTimeout: 60,
      restartPolicyType: "ALWAYS",
      // Time between SIGTERM and SIGKILL, so the instance can save its Gateway
      // session and release the leader lock.
      drainingSeconds: 30,
    },
    env: {
      DATABASE_URL: db.env.DATABASE_URL, // private network URL
      LOG_FORMAT: "json",
      RUST_LOG: "info,sqlx=warn,twilight_http_ratelimiting=warn",
      DISCORD_BOT_TOKEN: preserve(), // secret: set with `railway variable set`
      OPENROUTER_DECISION_API_KEY: preserve(), // secret, for Jev (Decide) via OpenRouter; optional
      // Set this to http://${{agent.RAILWAY_PRIVATE_DOMAIN}}:8081 once the agent
      // service has OPENROUTER_CHAT_API_KEY; unset, nothing is dispatched.
      AGENT_HOST_URL: preserve(),
      AGENT_HOST_TOKEN: "${{shared.AGENT_HOST_TOKEN}}",
      DISCORD_GUILD_IDS: "1358240830887952575,348273727298863104",
    },
  });

  return project("kiki", { resources: [db, dbVolume, agent, ingester] });
});
