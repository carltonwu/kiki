// Railway infrastructure for Kiki. Review changes with `railway config plan`,
// then apply with `railway config apply`.
import { defineRailway, github, postgres, preserve, project, service } from "railway/iac";

// TODO: set to the GitHub repo this monorepo is pushed to.
const REPO = "OWNER/kiki";

export default defineRailway(() => {
  const db = postgres("postgres");

  const ingester = service("ingester", {
    // No rootDirectory: the build needs the whole Cargo workspace.
    source: github(REPO, { branch: "main" }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "services/ingester/Dockerfile",
      // Only redeploy when something the ingester is built from changes.
      watchPatterns: ["services/ingester/**", "Cargo.toml", "Cargo.lock"],
    },
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
      RUST_LOG: "info,sqlx=warn",
      DISCORD_BOT_TOKEN: preserve(), // secret: set with `railway variable set`
      DISCORD_GUILD_ID: preserve(),
    },
  });

  return project("kiki", { resources: [db, ingester] });
});
