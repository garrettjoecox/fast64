import bpy
import os
import struct
from ...game_data import game_data

from mathutils import Matrix
from bpy.types import Object
from ...f3d.f3d_gbi import DLFormat, TextureExportSettings
from ..model_classes import OOTModel
from ..f3d_writer import writeTextureArraysNew, writeTextureArraysExisting1D
from .scene import Scene
from .decomp_edit import Files

from ...utility import (
    PluginError,
    checkObjectReference,
    unhideAllAndGetHiddenState,
    restoreHiddenState,
    toAlnum,
    readFile,
    writeFile,
)

from ..utility import (
    ExportInfo,
    OOTObjectCategorizer,
    ootDuplicateHierarchy,
    ootCleanupScene,
    getSceneDirFromLevelName,
    ootGetPath,
)


def writeTextureArraysExistingScene(fModel: OOTModel, exportPath: str, sceneInclude: str):
    drawConfigPath = os.path.join(exportPath, "src/code/z_scene_table.c")
    drawConfigData = readFile(drawConfigPath)
    newData = drawConfigData

    if f'#include "{sceneInclude}"' not in newData:
        additionalIncludes = f'#include "{sceneInclude}"\n'
    else:
        additionalIncludes = ""

    for flipbook in fModel.flipbooks:
        if flipbook.exportMode == "Array":
            newData = writeTextureArraysExisting1D(newData, flipbook, additionalIncludes)
        else:
            raise PluginError("Scenes can only use array flipbooks.")

    if newData != drawConfigData:
        writeFile(drawConfigPath, newData)


class SceneExport:
    """This class is the main exporter class, it handles generating the C data and writing the files"""

    @staticmethod
    def create_scene(originalSceneObj: Object, transform: Matrix, exportInfo: ExportInfo) -> Scene:
        """Returns and creates scene data"""
        # init
        if originalSceneObj.type != "EMPTY" or originalSceneObj.ootEmptyType != "Scene":
            raise PluginError(f'{originalSceneObj.name} is not an empty with the "Scene" empty type.')

        if bpy.context.scene.exportHiddenGeometry:
            hiddenState = unhideAllAndGetHiddenState(bpy.context.scene)

        # Don't remove ignore_render, as we want to reuse this for collision
        sceneObj, allObjs = ootDuplicateHierarchy(originalSceneObj, None, True, OOTObjectCategorizer())

        if bpy.context.scene.exportHiddenGeometry:
            restoreHiddenState(hiddenState)

        try:
            sceneName = f"{toAlnum(exportInfo.name)}_scene"
            newScene = Scene.new(
                sceneName,
                sceneObj,
                transform,
                exportInfo.useMacros,
                exportInfo.saveTexturesAsPNG,
                OOTModel(f"{sceneName}_dl", DLFormat.Static, False),
            )
            newScene.validateScene()

        except Exception as e:
            raise Exception(str(e))
        finally:
            ootCleanupScene(originalSceneObj, allObjs)

        return newScene

    @staticmethod
    def export(originalSceneObj: Object, transform: Matrix, exportInfo: ExportInfo):
        """Main function"""
        # circular import fixes
        from .decomp_edit.config import Config

        game_data.z64.update(bpy.context, None)

        checkObjectReference(originalSceneObj, "Scene object")
        scene = SceneExport.create_scene(originalSceneObj, transform, exportInfo)

        isCustomExport = exportInfo.isCustomExportPath
        exportPath = exportInfo.exportPath
        sceneName = exportInfo.name

        exportSubdir = ""
        if exportInfo.customSubPath is not None:
            exportSubdir = exportInfo.customSubPath
        if not isCustomExport and exportInfo.customSubPath is None:
            exportSubdir = os.path.dirname(getSceneDirFromLevelName(sceneName))

        sceneInclude = exportSubdir + "/" + sceneName + "/"
        path = ootGetPath(exportPath, isCustomExport, exportSubdir, sceneName, True, True)
        textureExportSettings = TextureExportSettings(False, exportInfo.saveTexturesAsPNG, sceneInclude, path)

        sceneFile = scene.getNewSceneFile(path, exportInfo.isSingleFile, textureExportSettings)

        if not isCustomExport:
            writeTextureArraysExistingScene(scene.model, exportPath, sceneInclude + sceneName + "_scene.h")
        else:
            textureArrayData = writeTextureArraysNew(scene.model, None)
            sceneFile.sceneTextures += textureArrayData.source
            sceneFile.header += textureArrayData.header

        sceneFile.write()
        for room in scene.rooms.entries:
            room.roomShape.copy_bg_images(path)

        if not isCustomExport:
            Files.add_scene_edits(exportInfo, scene, sceneFile)

        hackerootBootOption = exportInfo.hackerootBootOption
        if hackerootBootOption is not None and hackerootBootOption.bootToScene:
            Config.setBootupScene(
                os.path.join(exportPath, "include/config/config_debug.h")
                if not isCustomExport
                else os.path.join(path, "config_bootup.h"),
                f"ENTR_{sceneName.upper()}_{hackerootBootOption.spawnIndex}",
                hackerootBootOption,
            )

    @staticmethod
    def export_o2r(originalSceneObj: Object, transform: Matrix, exportInfo: ExportInfo):
        """Export scene assets in O2R binary format (writes textures, materials and meshes).

        This mirrors the C export but writes the FModel resources as O2R binaries into
        the target `objects/<sceneName>` folder under the export path.
        """
        from ...game_data import game_data as _game_data

        _game_data.z64.update(bpy.context, None)

        checkObjectReference(originalSceneObj, "Scene object")
        scene = SceneExport.create_scene(originalSceneObj, transform, exportInfo)

        isCustomExport = exportInfo.isCustomExportPath
        exportPath = exportInfo.exportPath
        sceneName = exportInfo.name

        exportSubdir = ""
        if exportInfo.customSubPath is not None:
            exportSubdir = exportInfo.customSubPath
        if not isCustomExport and exportInfo.customSubPath is None:
            exportSubdir = os.path.dirname(getSceneDirFromLevelName(sceneName))

        print("Exporting scene assets to O2R binary format...")

        # build output folder: scenes/nonmq/<sceneName>
        folderPath = os.path.join("scenes/nonmq", sceneName)
        exportFolderPath = os.path.join(exportPath, folderPath)
        if not os.path.exists(exportFolderPath):
            os.makedirs(exportFolderPath)

        sceneHeader = bytearray(0)

        # Write OTR Header
        # I    - Endianness
        # I    - Resource Type
        # I    - Game Version
        # Q    - Magic ID
        # I    - Resource Version
        # QI   - Empty space
        # QQQI - Fill until 64 bytes
        sceneHeader.extend(struct.pack("<IIIQIQIQQQI", 0, 0x4F524F4D, 0, 0xDEADBEEFDEADBEEF, 0, 0, 0, 0, 0, 0, 0))
        sceneHeader.extend(struct.pack("<I", 8)) # Amount of commands

        # SCENE_CMD_COL_HEADER
        sceneHeader.extend(struct.pack("<I", 0x03)) # SCENE_CMD_COL_HEADER
        colPath = os.path.join(folderPath, scene.colHeader.name)
        # For windows paths, replace backslashes with forward slashes
        colPath = colPath.replace("\\", "/")
        sceneHeader.extend(struct.pack("<I", len(colPath)))
        sceneHeader.extend(colPath.encode())

        collisionFile = bytearray(0)
        
        # Write OTR Header
        # I    - Endianness
        # I    - Resource Type
        # I    - Game Version
        # Q    - Magic ID
        # I    - Resource Version
        # QI   - Empty space
        # QQQI - Fill until 64 bytes
        collisionFile.extend(struct.pack("<IIIQIQIQQQI", 0, 0x4F434F4C, 0, 0xDEADBEEFDEADBEEF, 0, 0, 0, 0, 0, 0, 0))
        collisionFile.extend(struct.pack(
            "<hhhhhh",
            scene.colHeader.minBounds[0],
            scene.colHeader.minBounds[1],
            scene.colHeader.minBounds[2],
            scene.colHeader.maxBounds[0],
            scene.colHeader.maxBounds[1],
            scene.colHeader.maxBounds[2],
        ))

        collisionFile.extend(struct.pack("<I", len(scene.colHeader.vertices.vertexList)))
        for vertex in scene.colHeader.vertices.vertexList:
            collisionFile.extend(struct.pack("<hhh", vertex.pos[0], vertex.pos[1], vertex.pos[2]))

        collisionFile.extend(struct.pack("<I", len(scene.colHeader.collisionPoly.polyList)))
        for poly in scene.colHeader.collisionPoly.polyList:
            print(f"Exporting collision poly: Type={poly.type}, Indices={poly.indices}, Normal={poly.normal}, Dist={poly.dist}")
            collisionFile.extend(struct.pack(
                "<hhhhhhhH",
                int(poly.type),
                int(poly.indices[0]),
                int(poly.indices[1]),
                int(poly.indices[2]),
                int(poly.normal[0]),
                int(poly.normal[1]),
                int(poly.normal[2]),
                max(min(int(poly.dist), 65535), 0),
            ))
        
        collisionFile.extend(struct.pack("<I", len(scene.colHeader.surfaceType.surfaceTypeList)))
        for surfaceType in scene.colHeader.surfaceType.surfaceTypeList:
            collisionFile.extend(struct.pack("<II", surfaceType.getSurfaceType0Binary(), surfaceType.getSurfaceType1Binary()))

        # TODO: camData
        collisionFile.extend(struct.pack("<I", len(scene.colHeader.bgCamInfo.bgCamInfoList)))
        for camInfo in scene.colHeader.bgCamInfo.bgCamInfoList:
            collisionFile.extend(struct.pack(
                "<HHI",
                int(camInfo.setting, 0),
                0,
                0,
            ))
        collisionFile.extend(struct.pack("<I", 0))        

        # TODO: waterBoxes
        collisionFile.extend(struct.pack("<I", 0))

        with open(os.path.join(exportFolderPath, scene.colHeader.name), "wb") as f:
            f.write(collisionFile)

        # SCENE_CMD_ROOM_LIST
        sceneHeader.extend(struct.pack("<I", 0x04)) # SCENE_CMD_ROOM_LIST
        sceneHeader.extend(struct.pack("<I", len(scene.rooms.entries)))
        for room in scene.rooms.entries:
            roomPath = os.path.join(folderPath, room.name)
            # For windows paths, replace backslashes with forward slashes
            roomPath = roomPath.replace("\\", "/")
            sceneHeader.extend(struct.pack("<I", len(roomPath)))
            sceneHeader.extend(roomPath.encode())
            sceneHeader.extend(struct.pack("<I", 0))
            sceneHeader.extend(struct.pack("<I", 0))

        # SetActorCutsceneList
        sceneHeader.extend(struct.pack("<I", 0x1B)) # SetActorCutsceneList
        sceneHeader.extend(struct.pack("<I", 0))

        # SetCsCamera
        sceneHeader.extend(struct.pack("<I", 0x2)) # SetCsCamera
        sceneHeader.extend(struct.pack("<I", 1)) # size
        sceneHeader.extend(struct.pack("<H", 31)) # type
        sceneHeader.extend(struct.pack("<H", 3)) # numPoints
        sceneHeader.extend(struct.pack("<hhh", 158, 100, 312))
        sceneHeader.extend(struct.pack("<hhh", 1820, 4916, 0))
        sceneHeader.extend(struct.pack("<hhh", -1,     -1,     -1))

        # SCENE_CMD_SPECIAL_FILES
        sceneHeader.extend(struct.pack("<I", 0x07)) # SCENE_CMD_SPECIAL_FILES
        sceneHeader.extend(struct.pack("<BH", 0, 3))

        # SCENE_CMD_SKYBOX_SETTINGS
        sceneHeader.extend(struct.pack("<I", 0x11)) # SCENE_CMD_SKYBOX_SETTINGS
        sceneHeader.extend(struct.pack(
            "<BBBB",
            0,
            0,
            0,
            1,
        ))

        # SCENE_CMD_ENTRANCE_LIST
        sceneHeader.extend(struct.pack("<I", 0x06)) # SCENE_CMD_ENTRANCE_LIST
        sceneHeader.extend(struct.pack("<I", len(scene.mainHeader.spawns.entries)))
        for spawn in scene.mainHeader.spawns.entries:
            sceneHeader.extend(struct.pack(
                "<bb",
                spawn.spawnIndex,
                spawn.roomIndex,
            ))

        # SCENE_CMD_SPAWN_LIST
        sceneHeader.extend(struct.pack("<I", 0x00)) # SCENE_CMD_SPAWN_LIST
        sceneHeader.extend(struct.pack("<I", len(scene.mainHeader.entranceActors.entries)))
        for actor in scene.mainHeader.entranceActors.entries:
            sceneHeader.extend(struct.pack(
                "<hhhhhhhh",
                0,
                actor.pos[0],
                actor.pos[1],
                actor.pos[2],
                0,
                0,
                0,
                int(actor.params, 16),
            ))

        # SCENE_CMD_END
        sceneHeader.extend(struct.pack("<I", 0x14)) # SCENE_CMD_END
        with open(os.path.join(exportFolderPath, sceneName), "wb") as f:
            f.write(sceneHeader)

        for room in scene.rooms.entries:
            roomHeader = bytearray(0)

            # Write OTR Header
            # I    - Endianness
            # I    - Resource Type
            # I    - Game Version
            # Q    - Magic ID
            # I    - Resource Version
            # QI   - Empty space
            # QQQI - Fill until 64 bytes
            roomHeader.extend(struct.pack("<IIIQIQIQQQI", 0, 0x4F524F4D, 0, 0xDEADBEEFDEADBEEF, 0, 0, 0, 0, 0, 0, 0))
            roomHeader.extend(struct.pack("<I", 1 + 4 + 1)) # Amount of commands

            # SCENE_CMD_ROOM_SHAPE
            roomHeader.extend(struct.pack("<I", 0x0A)) # SCENE_CMD_ROOM_SHAPE
            roomHeader.extend(struct.pack("<B", 0x1)) # Data?

            roomType = 0
            print(f"Exporting room type: {room.roomShape.get_type()}")
            if room.roomShape.get_type() == "ROOM_SHAPE_TYPE_IMAGE":
                roomType = 1
            elif room.roomShape.get_type() == "ROOM_SHAPE_TYPE_CULLABLE":
                roomType = 2
            elif room.roomShape.get_type() == "ROOM_SHAPE_TYPE_NONE":
                roomType = 3
            roomHeader.extend(struct.pack("<B", roomType)) # Mesh Type
        
            roomHeader.extend(struct.pack("<B", len(room.roomShape.dl_entries)))

            for dlEntry in room.roomShape.dl_entries:
                if roomType == 2:
                    roomHeader.extend(struct.pack(
                        "<Bhhhh",
                        roomType,
                        dlEntry.bounds_sphere_center[0], 
                        dlEntry.bounds_sphere_center[1], 
                        dlEntry.bounds_sphere_center[2],
                        dlEntry.bounds_sphere_radius
                    ))
                if dlEntry.opaque is not None:
                    dlPath = os.path.join(folderPath, dlEntry.opaque.name)
                    # For windows paths, replace backslashes with forward slashes
                    dlPath = dlPath.replace("\\", "/")
                    roomHeader.extend(struct.pack("<I", len(dlPath)))
                    roomHeader.extend(dlPath.encode())
                    with open(os.path.join(exportFolderPath, dlEntry.opaque.name), "wb") as f:
                        f.write(dlEntry.opaque.toO2R(folderPath))
                else:
                    roomHeader.extend(struct.pack("<I", 0))

                if dlEntry.transparent is not None:
                    dlPath = os.path.join(folderPath, dlEntry.transparent.name)
                    # For windows paths, replace backslashes with forward slashes
                    dlPath = dlPath.replace("\\", "/")
                    roomHeader.extend(struct.pack("<I", len(dlPath)))
                    roomHeader.extend(dlPath.encode())
                    with open(os.path.join(exportFolderPath, dlEntry.transparent.name), "wb") as f:
                        f.write(dlEntry.transparent.toO2R(folderPath))
                else:
                    roomHeader.extend(struct.pack("<I", 0))

            # SCENE_CMD_ECHO_SETTINGS
            roomHeader.extend(struct.pack("<I", 0x16)) # SCENE_CMD_ECHO_SETTINGS
            roomHeader.extend(struct.pack("<B", int(room.mainHeader.infos.echo, 0)))

            # SCENE_CMD_ROOM_BEHAVIOR
            roomHeader.extend(struct.pack("<I", 0x08)) # SCENE_CMD_ROOM_BEHAVIOR
            roomHeader.extend(struct.pack(
                "<BBBBBB",
                0,
                0,
                room.mainHeader.infos.showInvisActors,
                room.mainHeader.infos.disableWarpSongs,
                room.mainHeader.infos.enable_pos_lights,
                room.mainHeader.infos.enable_storm,
            ))

            # SCENE_CMD_SKYBOX_DISABLES
            roomHeader.extend(struct.pack("<I", 0x12)) # SCENE_CMD_SKYBOX_DISABLES
            roomHeader.extend(struct.pack(
                "<BB",
                room.mainHeader.infos.disableSky,
                room.mainHeader.infos.disableSunMoon,
            ))

            # SCENE_CMD_TIME_SETTINGS
            roomHeader.extend(struct.pack("<I", 0x10)) # SCENE_CMD_TIME_SETTINGS
            roomHeader.extend(struct.pack(
                "<BBB",
                room.mainHeader.infos.hour,
                room.mainHeader.infos.minute,
                room.mainHeader.infos.timeSpeed,
            ))

            # SCENE_CMD_END
            roomHeader.extend(struct.pack("<I", 0x14)) # SCENE_CMD_END

            with open(os.path.join(exportFolderPath, room.name), "wb") as f:
                f.write(roomHeader)

            # Write all textures (FImage -> O2R)
            if room.roomShape is not None and room.roomShape.model is not None:
                for _, fImage in room.roomShape.model.textures.items():
                    print(f"Exporting texture: {fImage.name}")
                    with open(os.path.join(exportFolderPath, fImage.name), "wb") as f:
                        f.write(fImage.toO2R(folderPath))
                # Write materials (material + revert)
                for _, (fMaterial, _) in room.roomShape.model.materials.items():
                    if fMaterial.material is not None:
                        print(f"Exporting material: {fMaterial.material.name}")
                        with open(os.path.join(exportFolderPath, fMaterial.material.name), "wb") as f:
                            f.write(fMaterial.material.toO2R(folderPath))

                    if fMaterial.revert is not None:
                        print(f"Exporting revert material: {fMaterial.revert.name}")
                        with open(os.path.join(exportFolderPath, fMaterial.revert.name), "wb") as f:
                            f.write(fMaterial.revert.toO2R(folderPath))
                
                # Write meshes, their DLs, tri lists and vertex lists
                for name, mesh in room.roomShape.model.meshes.items():
                    if mesh.draw is not None:
                        print(f"Exporting mesh: {mesh.name}")
                        meshName = mesh.name
                        with open(os.path.join(exportFolderPath, meshName), "wb") as f:
                            f.write(mesh.draw.toO2R(folderPath))

                        for triGroup in mesh.triangleGroups:
                            if triGroup.triList is not None:
                                with open(os.path.join(exportFolderPath, triGroup.triList.name), "wb") as f:
                                    f.write(triGroup.triList.toO2R(folderPath))

                            if triGroup.vertexList is not None:
                                vertexListName = triGroup.vertexList.name
                                with open(os.path.join(exportFolderPath, vertexListName), "wb") as f:
                                    f.write(triGroup.vertexList.toO2R(folderPath))
