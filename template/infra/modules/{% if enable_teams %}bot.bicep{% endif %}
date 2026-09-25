// Azure Bot for Microsoft Teams. Secretless: the bot's identity is the service's user-assigned managed
// identity (msaAppType UserAssignedMSI), the same one the Container App runs as.
param name string
param tags object = {}
param displayName string
@description('Public URL of the bot endpoint, https://<app>/api/messages.')
param endpoint string
param identityClientId string
param identityResourceId string
param tenantId string = tenant().tenantId
@allowed(['F0', 'S1'])
@description('F0 is free with a message quota on premium channels; Teams is a standard channel.')
param sku string = 'F0'

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: name
  location: 'global'
  kind: 'azurebot'
  tags: tags
  sku: { name: sku }
  properties: {
    displayName: displayName
    endpoint: endpoint
    msaAppId: identityClientId
    msaAppType: 'UserAssignedMSI'
    msaAppMSIResourceId: identityResourceId
    msaAppTenantId: tenantId
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource teams 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

output name string = bot.name
output appId string = identityClientId
