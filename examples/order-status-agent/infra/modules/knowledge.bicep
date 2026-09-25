// Knowledge for one service: its own Azure AI Search (so no other service can read its index), Document
// Intelligence for PDF/Office files, and a Blob container for documents managed outside git.
// Entra only everywhere (local auth off). The service identity can only READ the index; writing is for
// the ingestion identity (the deploy principal).
param namePrefix string
param location string
param tags object = {}
@description('Service managed identity: reads the index and documents, calls Document Intelligence.')
param servicePrincipalId string
@description('Identity that runs ingestion (azd / the deploy pipeline). Empty = no ingestion grants.')
param ingestPrincipalId string = ''
@allowed(['basic', 'standard'])
param searchSku string = 'basic'
@allowed(['disabled', 'free', 'standard'])
@description('Semantic ranker plan ("free" = limited monthly queries).')
param semanticSearch string = 'free'

var roleSearchIndexDataReader = '1407120a-92aa-4202-b7e9-c0e197c71c8f'
var roleSearchIndexDataContributor = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
var roleSearchServiceContributor = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
var roleBlobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var roleBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var roleCognitiveServicesUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var suffix = uniqueString(resourceGroup().id)

resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: take('${namePrefix}-srch-${suffix}', 60)
  location: location
  tags: tags
  sku: { name: searchSku }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    disableLocalAuth: true
    semanticSearch: semanticSearch
    publicNetworkAccess: 'enabled'
  }
}

resource docintel 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: take('${namePrefix}-di-${suffix}', 64)
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: take('${namePrefix}-di-${suffix}', 64)
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('kb${suffix}${toLower(replace(namePrefix, '-', ''))}', 24) // 3-24 lowercase letters and digits
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'knowledge'
  properties: { publicAccess: 'None' }
}

// ---------------------------------------------------------------- the service: read only
resource serviceSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, servicePrincipalId, roleSearchIndexDataReader)
  scope: search
  properties: {
    principalId: servicePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleSearchIndexDataReader)
  }
}

resource serviceDocintelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(docintel.id, servicePrincipalId, roleCognitiveServicesUser)
  scope: docintel
  properties: {
    principalId: servicePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleCognitiveServicesUser)
  }
}

resource serviceBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, servicePrincipalId, roleBlobDataReader)
  scope: container
  properties: {
    principalId: servicePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleBlobDataReader)
  }
}

// ---------------------------------------------------------------- ingestion: create the index, write it
resource ingestSearchService 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(ingestPrincipalId)) {
  name: guid(search.id, ingestPrincipalId, roleSearchServiceContributor)
  scope: search
  properties: {
    principalId: ingestPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleSearchServiceContributor)
  }
}

resource ingestSearchData 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(ingestPrincipalId)) {
  name: guid(search.id, ingestPrincipalId, roleSearchIndexDataContributor)
  scope: search
  properties: {
    principalId: ingestPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleSearchIndexDataContributor)
  }
}

resource ingestDocintel 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(ingestPrincipalId)) {
  name: guid(docintel.id, ingestPrincipalId, roleCognitiveServicesUser)
  scope: docintel
  properties: {
    principalId: ingestPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleCognitiveServicesUser)
  }
}

resource ingestBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(ingestPrincipalId)) {
  name: guid(container.id, ingestPrincipalId, roleBlobDataContributor)
  scope: container
  properties: {
    principalId: ingestPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleBlobDataContributor)
  }
}

output searchEndpoint string = 'https://${search.name}.search.windows.net'
output searchName string = search.name
output docintelEndpoint string = docintel.properties.endpoint
output containerUrl string = '${storage.properties.primaryEndpoints.blob}${container.name}'
