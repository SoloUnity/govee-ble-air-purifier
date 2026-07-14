# Development References

This repository contains a Home Assistant custom integration distributed through
the Home Assistant Community Store (HACS).

Use Home Assistant's developer documentation for integration architecture,
runtime behavior, entities, configuration, Bluetooth APIs, and testing. Use the
HACS documentation for repository packaging, validation, releases, and
distribution requirements.

## Home Assistant Development

- [Developer documentation](https://developers.home-assistant.io/)
- [Creating your first integration](https://developers.home-assistant.io/docs/creating_component_index/)
- [Integration file structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/)
- [Integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [Config flows](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)
- [Fetching data and coordinators](https://developers.home-assistant.io/docs/integration_fetching_data/)
- [Entity development](https://developers.home-assistant.io/docs/core/entity/)
- [Bluetooth integration development](https://developers.home-assistant.io/docs/core/bluetooth/)
- [Bluetooth API](https://developers.home-assistant.io/docs/core/bluetooth/api/)
- [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
- [Example custom integrations](https://github.com/home-assistant/example-custom-config/tree/master/custom_components)
- [Home Assistant Core integrations](https://github.com/home-assistant/core/tree/dev/homeassistant/components)

Home Assistant Core is often the best source for current implementation
patterns because its APIs and conventions evolve over time.

## HACS Publishing

- [Publishing overview](https://www.hacs.xyz/docs/publish/)
- [Integration repository requirements](https://www.hacs.xyz/docs/publish/integration/)
- [HACS GitHub Action](https://www.hacs.xyz/docs/publish/action/)
- [Requirements for default inclusion](https://www.hacs.xyz/docs/publish/include/)
- [HACS manifest configuration](https://www.hacs.xyz/docs/publish/start/#hacsjson)
- [Reference integration template](https://github.com/custom-components/blueprint)

## Repository Focus

The most relevant guidance for this integration covers:

- Home Assistant Bluetooth APIs
- `DataUpdateCoordinator` polling and state management
- Config entries and options flows
- Fan and sensor entities
- Diagnostics and redaction
- HACS integration packaging
- HACS Action and hassfest validation

When documentation and examples differ, prefer current Home Assistant developer
documentation and recently maintained Home Assistant Core integrations.
