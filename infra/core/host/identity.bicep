param name string
param location string = resourceGroup().location
param tags object = {}

resource userIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
  tags: tags
}

output identityId string = userIdentity.id
output identityName string = userIdentity.name
output principalId string = userIdentity.properties.principalId
output clientId string = userIdentity.properties.clientId
