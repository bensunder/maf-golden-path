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
param cosmosAccountName string
param cosmosDatabase string = 'agentkit'

@description('Entra app role an approver must hold for high-impact tools. Empty = the requester confirms.')
param approverRole string = ''

@description('Client id of the Entra app registration that protects this API (Easy Auth). Required for prod.')
param authClientId string = ''

@description('Knowledge: identity that runs ingestion (azd sets AZURE_PRINCIPAL_ID to whoever runs azd: you, or the deploy pipeline).')
param ingestPrincipalId string = ''

@allowed(['basic', 'standard'])
param searchSku string = 'basic'

@description('Knowledge: embedding deployment behind the AI gateway.')
param knowledgeEmbeddingModel string = 'text-embedding-3-small'

@description('Teams: Entra group whose members may approve in Teams. The identity needs GroupMember.Read.All (docs/channels.md).')
param teamsApproverGroupId string = ''

@description('Teams: channel id (19:...@thread.tacv2) that receives approval cards. Empty = the requester\'s chat.')
param teamsApprovalsChannelId string = ''

@allowed(['F0', 'S1'])
param botSku string = 'F0'

param minReplicas int = 1
@description('Sessions live in Cosmos DB and are locked per conversation, so any replica can serve any request.')
param maxReplicas int = 5

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

module sessions 'modules/sessions.bicep' = {
  name: 'sessions-${serviceName}'
  scope: resourceGroup(platformResourceGroup)
  params: {
    cosmosAccountName: cosmosAccountName
    databaseName: cosmosDatabase
    containerName: '${serviceName}-${agentEnvironment}-sessions'
    principalId: identity.outputs.principalId
  }
}

module knowledge 'modules/knowledge.bicep' = {
  name: 'knowledge'
  scope: rg
  params: {
    namePrefix: take('${serviceName}-${agentEnvironment}', 30)
    location: location
    tags: tags
    servicePrincipalId: identity.outputs.principalId
    ingestPrincipalId: ingestPrincipalId
    searchSku: searchSku
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
    browserSignIn: true
    anonymousPaths: ['/api/messages']
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
      { name: 'AGENTKIT_SESSION_STORE', value: 'cosmos' }
      { name: 'AGENTKIT_COSMOS_ENDPOINT', value: sessions.outputs.endpoint }
      { name: 'AGENTKIT_COSMOS_DATABASE', value: cosmosDatabase }
      { name: 'AGENTKIT_COSMOS_CONTAINER', value: sessions.outputs.containerName }
      { name: 'AGENTKIT_APPROVER_ROLE', value: approverRole }
      { name: 'AGENTKIT_KNOWLEDGE_SEARCH_ENDPOINT', value: knowledge.outputs.searchEndpoint }
      { name: 'AGENTKIT_KNOWLEDGE_INDEX', value: 'knowledge' }
      { name: 'AGENTKIT_KNOWLEDGE_EMBEDDING_MODEL', value: knowledgeEmbeddingModel }
      { name: 'AGENTKIT_KNOWLEDGE_DOCINTEL_ENDPOINT', value: knowledge.outputs.docintelEndpoint }
      { name: 'AGENTKIT_TEAMS_APP_ID', value: identity.outputs.clientId }
      { name: 'AGENTKIT_TEAMS_TENANT_ID', value: tenant().tenantId }
      { name: 'AGENTKIT_TEAMS_APPROVER_GROUP_ID', value: teamsApproverGroupId }
      { name: 'AGENTKIT_TEAMS_APPROVALS_CHANNEL_ID', value: teamsApprovalsChannelId }
      // Without Easy Auth there is no trusted user header, so every call is rejected (secure default).
      { name: 'AGENTKIT_REQUIRE_USER', value: 'true' }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-connection-string' }
    ]
    appInsightsConnectionString: platformAccess.outputs.appInsightsConnectionString
  }
}
module bot 'modules/bot.bicep' = {
  name: 'bot'
  scope: rg
  params: {
    name: take('bot-${serviceName}-${agentEnvironment}-${uniqueString(rg.id)}', 64)
    displayName: 'Order Status Agent'
    endpoint: '${app.outputs.uri}/api/messages'
    identityClientId: identity.outputs.clientId
    identityResourceId: identity.outputs.id
    sku: botSku
    tags: tags
  }
}

// azd reads these
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = platformAccess.outputs.registryServer
output SERVICE_API_NAME string = app.outputs.name
output SERVICE_API_URI string = app.outputs.uri
output SERVICE_API_IDENTITY_PRINCIPAL_ID string = identity.outputs.principalId
output SERVICE_API_EASY_AUTH_ENABLED bool = !empty(authClientId)
output AGENTKIT_KNOWLEDGE_SEARCH_ENDPOINT string = knowledge.outputs.searchEndpoint
output AGENTKIT_KNOWLEDGE_DOCINTEL_ENDPOINT string = knowledge.outputs.docintelEndpoint
output AGENTKIT_KNOWLEDGE_CONTAINER_URL string = knowledge.outputs.containerUrl
output TEAMS_BOT_APP_ID string = bot.outputs.appId
output TEAMS_BOT_NAME string = bot.outputs.name
