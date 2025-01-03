---
name: Bug report
about: Create a report when you think you've found a bug in the Solcast integration
title: ''
labels: ''
assignees: ''

---

## Describe the bug

A clear and concise description of what the bug is.

## To Reproduce

Steps to reproduce the behavior:

1. Go to '...'
2. Click on '....'
3. Scroll down to '....'
4. See error

## Expected behavior

A clear and concise description of what you expected to happen.

## Screenshots

If applicable, add screenshots to help explain your problem.

## Logs

I confirm:

- [ ] I have attached **debug** logs
- [ ] I have embedded **debug** logs in the issue description (enclosed in tick marks ``` for proper formatting)
- [ ] Confirmed **debug** logs are not required for this issue

Make sure you include logs from HA listing the output from the Solcast integration showing the error - this is particularly useful in debugging issues and helping to determine whether the issue is with the integration or the Solcast service

To add detailed debug information, add the following to your configuration.yaml and restart HA:

``` yaml
logger:
  default: warn
  logs:
    custom_components.solcast_solar: debug
```

To inspect and collect debug logs examine `/config/home-assistant.log` using File Editor or Visual Studio Code Server.

If you are using docker, it sometimes can be easier to gather logs using `docker compose logs -n 500 -f homeassistant` or similar

## Solcast Integration Version

- Integration Version [e.g. 4.0.29]

## Desktop (please complete the following information)

- OS: [e.g. iOS]
- Browser [e.g. chrome, safari]
- Version [e.g. 22]

## Smartphone (please complete the following information)

- Device: [e.g. iPhone6]
- OS: [e.g. iOS8.1]
- Browser [e.g. stock browser, safari]
- Version [e.g. 22]

## Additional context

Add any other context about the problem here.
