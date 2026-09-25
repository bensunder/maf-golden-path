// agentkit shared platform: deployed once per environment by the platform team.
// Every agent service generated from the template deploys *into* this platform.
//
//   az deployment group create -g rg-agentkit-dev -f infra/platform/main.bicep -p infra/platform/main.parameters.json
targetScope = 'resourceGroup'

@description('Short prefix for resource names, e.g. "akdev".')
@minLength(3)
@maxLength(12)
param namePrefix string

param location string = resourceGroup().location

@description('API Management SKU. BasicV2 is the cheapest tier that supports llm-token-limit.')
@allowed(['Developer', 'BasicV2', 'StandardV2', 'Premium'])
param apimSku string = 'BasicV2'

param apimPublisherEmail string
param apimPublisherName string = 'Platform Engineering'

@description('Tokens per minute per calling identity (each agent service has its own).')
param tokensPerMinutePerCaller int = 20000

@description('Token quota per calling identity per quota period.')
param tokenQuotaPerCaller int = 5000000

@allowed(['Hourly', 'Daily', 'Weekly', 'Monthly', 'Yearly'])
param tokenQuotaPeriod string = 'Monthly'

@description('Create an Azure OpenAI account behind the gateway. Set false to point at an existing endpoint.')
param deployOpenAI bool = true

@description('Existing Azure OpenAI endpoint (https://<name>.openai.azure.com/) when deployOpenAI is false.')
param existingOpenAIEndpoint string = ''

param openAIDeploymentName string = 'gpt-4.1-mini'
param openAIModelName string = 'gpt-4.1-mini'
param openAIModelVersion string = '2025-04-14'
@description('Deployment capacity in thousands of tokens per minute.')
param openAICapacity int = 50

param tags object = {}

var suffix = uniqueString(resourceGroup().id)
var allTags = union(tags, { 'agentkit-platform': namePrefix })

// Built-in role definition ids
var roleCognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

// ---------------------------------------------------------------- observability
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs-${suffix}'
  location: location
  tags: allTags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-appi-${suffix}'
  location: location
  kind: 'web'
  tags: allTags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    // Required for llm-emit-token-metric dimensions. Accepted by ARM but missing from Bicep's type definitions.
    #disable-next-line BCP037
    CustomMetricsOptedInType: 'WithDimensions'
  }
}

// ---------------------------------------------------------------- runtime
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: toLower(replace('${namePrefix}acr${suffix}', '-', ''))
  location: location
  tags: allTags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-cae-${suffix}'
  location: location
  tags: allTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------- safety
resource contentSafety 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: '${namePrefix}-cs-${suffix}'
  location: location
  tags: allTags
  kind: 'ContentSafety'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${namePrefix}-cs-${suffix}'
    disableLocalAuth: true // Entra ID only; services use their managed identity
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------- models
resource openAI 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployOpenAI) {
  name: '${namePrefix}-aoai-${suffix}'
  location: location
  tags: allTags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${namePrefix}-aoai-${suffix}'
    disableLocalAuth: true // only the gateway's managed identity can call it
    publicNetworkAccess: 'Enabled'
  }
}

resource openAIDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployOpenAI) {
  parent: openAI
  name: openAIDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: openAICapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: openAIModelName
      version: openAIModelVersion
    }
  }
}

var openAIEndpoint = deployOpenAI ? openAI!.properties.endpoint : existingOpenAIEndpoint

// ---------------------------------------------------------------- AI gateway
resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: '${namePrefix}-apim-${suffix}'
  location: location
  tags: allTags
  sku: {
    name: apimSku
    capacity: 1
  }
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherEmail: apimPublisherEmail
    publisherName: apimPublisherName
  }
}

resource apimToOpenAI 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployOpenAI) {
  name: guid(openAI.id, apim.id, roleCognitiveServicesOpenAIUser)
  scope: openAI
  properties: {
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleCognitiveServicesOpenAIUser)
  }
}

resource apimLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsights.id
    credentials: {
      connectionString: appInsights.properties.ConnectionString
    }
  }
}

resource backend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apim
  name: 'aoai'
  properties: {
    protocol: 'http'
    url: '${endsWith(openAIEndpoint, '/') ? openAIEndpoint : '${openAIEndpoint}/'}openai'
    circuitBreaker: {
      rules: [
        {
          name: 'openai-throttling'
          failureCondition: {
            count: 3
            interval: 'PT1M'
            statusCodeRanges: [{ min: 429, max: 429 }]
          }
          tripDuration: 'PT1M'
          acceptRetryAfter: true
        }
      ]
    }
  }
}

resource openaiApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'openai'
  properties: {
    displayName: 'Azure OpenAI (agentkit gateway)'
    path: 'openai'
    protocols: ['https']
    subscriptionRequired: false // callers authenticate with Entra ID; see policy
    apiType: 'http'
  }
}

resource chatCompletions 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: openaiApi
  name: 'chat-completions'
  properties: {
    displayName: 'Chat completions'
    method: 'POST'
    urlTemplate: '/deployments/{deployment-id}/chat/completions'
    templateParameters: [{ name: 'deployment-id', type: 'string', required: true }]
  }
}

resource embeddings 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: openaiApi
  name: 'embeddings'
  properties: {
    displayName: 'Embeddings'
    method: 'POST'
    urlTemplate: '/deployments/{deployment-id}/embeddings'
    templateParameters: [{ name: 'deployment-id', type: 'string', required: true }]
  }
}

resource openaiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: openaiApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(replace(replace(replace(loadTextContent('policies/ai-gateway.xml'),
      '__TENANT_ID__', tenant().tenantId),
      '__TOKENS_PER_MINUTE__', string(tokensPerMinutePerCaller)),
      '__TOKEN_QUOTA__', string(tokenQuotaPerCaller)),
      '__QUOTA_PERIOD__', tokenQuotaPeriod)
  }
  dependsOn: [backend]
}

resource openaiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: openaiApi
  name: 'applicationinsights'
  properties: {
    loggerId: apimLogger.id
    metrics: true // required for llm-emit-token-metric
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    verbosity: 'information'
  }
}

// ---------------------------------------------------------------- outputs (feed these to services)
output AGENTKIT_PLATFORM_RESOURCE_GROUP string = resourceGroup().name
output AGENTKIT_GATEWAY_ENDPOINT string = apim.properties.gatewayUrl
output AGENTKIT_MODEL string = openAIDeploymentName
output AGENTKIT_CONTENT_SAFETY_ENDPOINT string = contentSafety.properties.endpoint
output AGENTKIT_CONTENT_SAFETY_NAME string = contentSafety.name
output AGENTKIT_CONTAINER_APPS_ENVIRONMENT_ID string = containerEnv.id
output AGENTKIT_REGISTRY_NAME string = registry.name
output AGENTKIT_REGISTRY_ENDPOINT string = registry.properties.loginServer
output AGENTKIT_APPINSIGHTS_NAME string = appInsights.name
