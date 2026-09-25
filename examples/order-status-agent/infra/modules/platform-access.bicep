// Least-privilege grants for the service identity on shared platform resources.
// Deployed at the platform resource group scope.
param principalId string
param registryName string
param contentSafetyName string
param appInsightsName string

var roleAcrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var roleCognitiveServicesUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource contentSafety 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: contentSafetyName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, roleAcrPull)
  scope: registry
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
  }
}

// Prompt Shields calls with Entra ID
resource contentSafetyUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(contentSafety.id, principalId, roleCognitiveServicesUser)
  scope: contentSafety
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleCognitiveServicesUser)
  }
}

output registryServer string = registry.properties.loginServer
output contentSafetyEndpoint string = contentSafety.properties.endpoint
@secure()
output appInsightsConnectionString string = appInsights.properties.ConnectionString
