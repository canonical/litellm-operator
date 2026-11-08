# LiteLLM Operator

## Description

The LiteLLM Operator deploys and operates the [LiteLLM](https://github.com/BerriAI/litellm) proxy, an OpenAI-compatible gateway to more than 100 LLM providers, on Kubernetes.

The charm is in development and has no release on Charmhub yet. This document describes each feature as it is delivered. The features planned are defined in [`requirements/`](requirements/).

## Usage

Bootstrap a Kubernetes controller to Juju (Juju 3.3+) and create a model:

```shell
juju add-model litellm
```

Deployment instructions follow with the first release.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how features are defined, built and tested.

## License

Copyright 2026 Canonical Ltd. The LiteLLM Operator is free software, distributed under the Apache Software License, version 2.0. See [LICENSE](LICENSE) for more information. LiteLLM itself is MIT licensed by BerriAI, except its `enterprise/` directory.
