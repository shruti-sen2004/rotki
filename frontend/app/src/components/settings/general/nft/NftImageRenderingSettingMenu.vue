<template>
  <VMenu offset-x left min-width="500" :close-on-content-click="false">
    <template #activator="{ on }">
      <VBtn icon v-on="on">
        <VIcon>mdi-cog</VIcon>
      </VBtn>
    </template>
    <div class="pa-4">
      <NftImageRenderingSetting />
    </div>
  </VMenu>
</template>
