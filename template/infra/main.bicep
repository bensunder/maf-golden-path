// Agent service infrastructure. Deploys into the shared agentkit platform (infra/platform in the kit repo).
//   azd up      (reads infra/main.parameters.json, values come from `azd env`)
targetScope = 'subscription'

@description('azd environment name, e.g. "orders-dev".')
@minLength(1)
@maxLength(40)
param environmentName string

param location string

@description('Service name used for resources, telemetry and gateway headers.')
param serviceName string

@description('Owning team (gateway metrics dimension, span attribute).')
param team string

@allowed(['dev', 'test', 'prod'])
param agentEnvironment string = 'dev'

@description('Resource group of the shared agentkit platform.')
param platformResourceGroup string
param containerAppsEnvironmentId string
param registryName string
param contentSafetyName string
param appInsightsName string
param gatewayEndpoint string
param model string

@description('Client id of the Entra app registration that protects this API (Easy Auth). Required for prod.')
param authClientId string = ''

param minReplicas int = 1
@description('Keep at 1 while sessions use the in-memory store; raise after configuring a shared SessionStore (Redis/Cosmos).')
param maxReplicas int = 1

var tags = { 'azd-env-name': environmentName, 'agentkit-team': team, 'agentkit-service': serviceName }

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    name: 'id-${serviceName}-${agentEnvironment}'
    location: location
    tags: tags
  }
}

module platformAccess 'modules/platform-access.bicep' = {
  name: 'platform-access-${serviceName}'
  scope: resourceGroup(platformResourceGroup)
  params: {
    principalId: identity.outputs.principalId
    registryName: registryName
    contentSafetyName: contentSafetyName
    appInsightsName: appInsightsName
  }
}

module app 'modules/container-app.bicep' = {
  name: 'container-app'
  scope: rg
  params: {
    name: take('ca-${serviceName}-${agentEnvironment}', 32)
    location: location
    tags: union(tags, { 'azd-service-name': 'api' })
    containerAppsEnvironmentId: containerAppsEnvironmentId
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    registryServer: platformAccess.outputs.registryServer
    authClientId: authClientId
    minReplicas: minReplicas
    maxReplicas: maxReplicas
    env: [
      { name: 'AGENTKIT_ENVIRONMENT', value: agentEnvironment }
      { name: 'AGENTKIT_SERVICE_NAME', value: serviceName }
      { name: 'AGENTKIT_TEAM', value: team }
      { name: 'AGENTKIT_GATEWAY_ENDPOINT', value: gatewayEndpoint }
      { name: 'AGENTKIT_MODEL', value: model }
      { name: 'AGENTKIT_AUTH_MODE', value: 'managed_identity' }
      { name: 'AGENTKIT_MANAGED_IDENTITY_CLIENT_ID', value: identity.outputs.clientId }
      { name: 'AZURE_CLIENT_ID', value: identity.outputs.clientId }
      { name: 'AGENTKIT_GUARDRAIL_MODE', value: 'prompt_shields' }
      { name: 'AGENTKIT_CONTENT_SAFETY_ENDPOINT', value: platformAccess.outputs.contentSafetyEndpoint }
      // Without Easy Auth there is no trusted user header, so every call is rejected (secure default).
      { name: 'AGENTKIT_REQUIRE_USER', value: 'true' }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-connection-string' }
    ]
    appInsightsConnectionString: platformAccess.outputs.appInsightsConnectionString
  }
}

// azd reads these
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = platformAccess.outputs.registryServer
output SERVICE_API_NAME string = app.outputs.name
output SERVICE_API_URI string = app.outputs.uri
output SERVICE_API_IDENTITY_PRINCIPAL_ID string = identity.outputs.principalId
output SERVICE_API_EASY_AUTH_ENABLED bool = !empty(authClientId)
