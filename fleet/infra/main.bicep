// The fleet view: one console for every agent service (docs/fleet.md). Deployed once per environment,
// next to the shared platform. Reuses the service template's modules.
//   cd fleet && azd up
targetScope = 'subscription'

@minLength(1)
@maxLength(40)
param environmentName string
param location string

@allowed(['dev', 'test', 'prod'])
param agentEnvironment string = 'dev'

param platformResourceGroup string
param containerAppsEnvironmentId string
param registryName string
param contentSafetyName string
param appInsightsName string

@description('Client id of the Entra app registration that protects the fleet view (Easy Auth, browser sign-in). Required: without it the fleet would be public.')
@minLength(36)
param authClientId string

@description('Agents to show besides discovered ones: "url|api://<app id>|name" entries separated by ";" (docs/fleet.md).')
param fleetAgents string = ''

@description('Also find agent services in this subscription by their agentkit-service tag (grants the fleet Reader).')
@allowed(['true', 'false'])
param discover string = 'true' // a string: azd substitutes environment values as text

@description('Entra app role people need to open the fleet view. Empty = anyone who can sign in to its app.')
param fleetRole string = ''

var tags = { 'azd-env-name': environmentName, 'agentkit-fleet': 'true', 'agentkit-environment': agentEnvironment }
var roleReader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module identity '../../template/infra/modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    name: 'id-agentkit-fleet-${agentEnvironment}'
    location: location
    tags: tags
  }
}

module platformAccess '../../template/infra/modules/platform-access.bicep' = {
  name: 'platform-access-fleet'
  scope: resourceGroup(platformResourceGroup)
  params: {
    principalId: identity.outputs.principalId
    registryName: registryName
    contentSafetyName: contentSafetyName
    appInsightsName: appInsightsName
    readTelemetry: true
    useContentSafety: false
  }
}

// Discovery searches Azure Resource Graph for tagged Container Apps: read-only, this subscription.
resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (discover == 'true') {
  name: guid(subscription().id, environmentName, 'agentkit-fleet', roleReader)
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleReader)
  }
}

module app '../../template/infra/modules/container-app.bicep' = {
  name: 'fleet'
  scope: rg
  params: {
    name: take('ca-fleet-${agentEnvironment}-${uniqueString(rg.id)}', 32)
    location: location
    tags: union(tags, { 'azd-service-name': 'fleet' })
    containerAppsEnvironmentId: containerAppsEnvironmentId
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    registryServer: platformAccess.outputs.registryServer
    authClientId: authClientId
    browserSignIn: true
    minReplicas: 1
    maxReplicas: 2
    env: [
      { name: 'AGENTKIT_ENVIRONMENT', value: agentEnvironment }
      { name: 'AGENTKIT_AUTH_MODE', value: 'managed_identity' }
      { name: 'AGENTKIT_MANAGED_IDENTITY_CLIENT_ID', value: identity.outputs.clientId }
      { name: 'AZURE_CLIENT_ID', value: identity.outputs.clientId }
      { name: 'AGENTKIT_REQUIRE_USER', value: 'true' }
      { name: 'AGENTKIT_FLEET_AGENTS', value: fleetAgents }
      { name: 'AGENTKIT_FLEET_DISCOVER', value: discover }
      { name: 'AGENTKIT_FLEET_SUBSCRIPTIONS', value: subscription().subscriptionId }
      { name: 'AGENTKIT_FLEET_ROLE', value: fleetRole }
      { name: 'AGENTKIT_CONSOLE_LOGS_RESOURCE', value: platformAccess.outputs.appInsightsId }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-connection-string' }
    ]
    appInsightsConnectionString: platformAccess.outputs.appInsightsConnectionString
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = platformAccess.outputs.registryServer
output SERVICE_FLEET_NAME string = app.outputs.name
output SERVICE_FLEET_URI string = app.outputs.uri
output SERVICE_FLEET_IDENTITY_PRINCIPAL_ID string = identity.outputs.principalId
output SERVICE_FLEET_IDENTITY_CLIENT_ID string = identity.outputs.clientId
