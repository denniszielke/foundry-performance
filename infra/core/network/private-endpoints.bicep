targetScope = 'resourceGroup'

@description('Private endpoints and private DNS zones for the Foundry account and its storage account.')
param location string = resourceGroup().location

@description('Tags that will be applied to all resources')
param tags object = {}

@description('Virtual network the private DNS zones are linked to')
param vnetId string

@description('Subnet that hosts the private endpoints')
param privateEndpointSubnetId string

@description('Name of the AI Services (Foundry) account')
param aiAccountName string

@description('Name of the storage account backing the Foundry project')
param storageAccountName string = ''

var aiServicesDnsZoneName = 'privatelink.services.ai.azure.com'
var openAiDnsZoneName = 'privatelink.openai.azure.com'
var cognitiveServicesDnsZoneName = 'privatelink.cognitiveservices.azure.com'
var storageDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'

var accountDnsZoneNames = [
  aiServicesDnsZoneName
  openAiDnsZoneName
  cognitiveServicesDnsZoneName
]

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiAccountName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = if (!empty(storageAccountName)) {
  name: storageAccountName
}

resource accountDnsZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for zone in accountDnsZoneNames: {
  name: zone
  location: 'global'
  tags: tags
}]

resource accountDnsZoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (zone, index) in accountDnsZoneNames: {
  parent: accountDnsZones[index]
  name: '${replace(zone, '.', '-')}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}]

resource aiAccountPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${aiAccountName}-private-endpoint'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${aiAccountName}-private-link-service-connection'
        properties: {
          privateLinkServiceId: aiAccount.id
          groupIds: [
            'account'
          ]
        }
      }
    ]
  }
}

resource aiAccountDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: aiAccountPrivateEndpoint
  name: '${aiAccountName}-dns-group'
  properties: {
    privateDnsZoneConfigs: [for (zone, index) in accountDnsZoneNames: {
      name: '${replace(zone, '.', '-')}-config'
      properties: {
        privateDnsZoneId: accountDnsZones[index].id
      }
    }]
  }
  dependsOn: [
    accountDnsZoneLinks
  ]
}

resource storageDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (!empty(storageAccountName)) {
  name: storageDnsZoneName
  location: 'global'
  tags: tags
}

resource storageDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (!empty(storageAccountName)) {
  parent: storageDnsZone
  name: '${replace(storageDnsZoneName, '.', '-')}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource storagePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (!empty(storageAccountName)) {
  name: '${storageAccountName}-private-endpoint'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${storageAccountName}-private-link-service-connection'
        properties: {
          privateLinkServiceId: storageAccount.id
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource storageDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (!empty(storageAccountName)) {
  parent: storagePrivateEndpoint
  name: '${storageAccountName}-dns-group'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: '${replace(storageDnsZoneName, '.', '-')}-config'
        properties: {
          privateDnsZoneId: storageDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    storageDnsZoneLink
  ]
}

output aiAccountPrivateEndpointId string = aiAccountPrivateEndpoint.id
output privateDnsZoneNames array = empty(storageAccountName) ? accountDnsZoneNames : union(accountDnsZoneNames, [storageDnsZoneName])
