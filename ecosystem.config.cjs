module.exports = {
  apps: [
    {
      name: "community-discord-bot",
      script: "bot.py",
      interpreter: ".venv/bin/python",
      cwd: __dirname,
      autorestart: true,
      restart_delay: 3000,
      max_restarts: 20,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
